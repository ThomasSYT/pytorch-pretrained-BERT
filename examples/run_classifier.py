# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HugginFace Inc.
# team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT finetuning runner."""

import os
import random
from tqdm import tqdm, trange
import tempfile

import numpy as np
import torch
from torch.utils.data import DataLoader, RandomSampler
from torch.utils.data.distributed import DistributedSampler

from pytorch_pretrained_bert.tokenization import BertTokenizer
from pytorch_pretrained_bert.modeling import (
    BertForSequenceClassification,
    BertConfig,
)
from pytorch_pretrained_bert.optimization import BertAdam
from pytorch_pretrained_bert.file_utils import PYTORCH_PRETRAINED_BERT_CACHE

import classifier_args
import classifier_data as data
from logger import logger
import pruning
from classifier_util import (
    evaluate,
    calculate_head_importance,
    analyze_nli,
    predict
)


def warmup_linear(x, warmup=0.002):
    if x < warmup:
        return x/warmup
    return 1.0 - x


def prepare_dry_run(args):
    args.no_cuda = True
    args.train_batch_size = 3
    args.eval_batch_size = 3
    args.do_train = True
    args.do_eval = True
    args.do_prune = True
    args.do_anal = True
    args.output_dir = tempfile.mkdtemp()
    return args


def main():
    # Arguments
    parser = classifier_args.get_base_parser()
    classifier_args.training_args(parser)
    classifier_args.fp16_args(parser)
    classifier_args.pruning_args(parser)
    classifier_args.eval_args(parser)
    classifier_args.analysis_args(parser)

    args = parser.parse_args()

    if args.dry_run:
        args = prepare_dry_run(args)

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device(
            "cuda" if torch.cuda.is_available() and not args.no_cuda
            else "cpu"
        )
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of
        # sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')
    logger.info(
        f"device: {device} n_gpu: {n_gpu}, "
        f"distributed training: {bool(args.local_rank != -1)}, "
        f"16-bits training: {args.fp16}"
    )

    if args.gradient_accumulation_steps < 1:
        raise ValueError(
            f"Invalid gradient_accumulation_steps parameter: "
            f"{args.gradient_accumulation_steps}, should be >= 1"
        )

    args.train_batch_size = int(
        args.train_batch_size
        / args.gradient_accumulation_steps
    )

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not (args.do_train or args.do_eval or args.do_prune or args.do_anal):
        raise ValueError(
            "At least one of `do_train`, `do_eval` or `do_prune` must be True."
        )
    out_dir_exists = os.path.exists(args.output_dir) and \
        os.listdir(args.output_dir)
    if out_dir_exists and args.do_train:
        raise ValueError(
            f"Output directory ({args.output_dir}) already exists and is not "
            "empty."
        )
    os.makedirs(args.output_dir, exist_ok=True)

    task_name = args.task_name.lower()

    if task_name not in data.processors:
        raise ValueError("Task not found: %s" % (task_name))

    processor = data.processors[task_name]()
    num_labels = data.num_labels_task[task_name]
    label_list = processor.get_labels()

    tokenizer = BertTokenizer.from_pretrained(
        args.bert_model, do_lower_case=args.do_lower_case)

    train_examples = None
    num_train_steps = None
    if args.do_train or args.do_prune:
        # Prepare training data
        if args.dry_run:
            train_examples = processor.get_dummy_train_examples(args.data_dir)
        else:
            train_examples = processor.get_train_examples(args.data_dir)
        train_data = data.prepare_tensor_dataset(
            train_examples,
            label_list,
            args.max_seq_length,
            tokenizer,
            verbose=args.verbose,
        )
        # Prepare data loader
        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(
            train_data,
            sampler=train_sampler,
            batch_size=args.train_batch_size
        )
        # Number of training steps
        num_train_steps = int(
            len(train_examples)
            / args.train_batch_size
            / args.gradient_accumulation_steps
        ) * args.num_train_epochs

    # Prepare model

    if args.dry_run:
        model = BertForSequenceClassification(
            BertConfig.dummy_config(len(tokenizer.vocab)),
            num_labels=num_labels
        )
    else:
        model = BertForSequenceClassification.from_pretrained(
            args.bert_model,
            cache_dir=PYTORCH_PRETRAINED_BERT_CACHE /
            f"distributed_{args.local_rank}",
            num_labels=num_labels
        )
    if args.fp16:
        model.half()
    model.to(device)
    if args.local_rank != -1:
        try:
            from apex.parallel import DistributedDataParallel as DDP
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex "
                "to use distributed and fp16 training."
            )

        model = DDP(model)
    elif n_gpu > 1:
        model = torch.nn.DataParallel(model)

    # Prepare optimizer
    param_optimizer = list(model.named_parameters())
    no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
    optimizer_grouped_parameters = [
        {'params': [p for n, p in param_optimizer if not any(
            nd in n for nd in no_decay)], 'weight_decay': 0.01},
        {'params': [p for n, p in param_optimizer if any(
            nd in n for nd in no_decay)], 'weight_decay': 0.0}
    ]
    t_total = num_train_steps
    if args.local_rank != -1:
        t_total = t_total // torch.distributed.get_world_size()
    if args.fp16:
        try:
            from apex.optimizers import FP16_Optimizer
            from apex.optimizers import FusedAdam
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex "
                "to use distributed and fp16 training."
            )

        optimizer = FusedAdam(optimizer_grouped_parameters,
                              lr=args.learning_rate,
                              bias_correction=False,
                              max_grad_norm=1.0)
        if args.loss_scale == 0:
            optimizer = FP16_Optimizer(optimizer, dynamic_loss_scale=True)
        else:
            optimizer = FP16_Optimizer(
                optimizer, static_loss_scale=args.loss_scale)

    else:
        optimizer = BertAdam(optimizer_grouped_parameters,
                             lr=args.learning_rate,
                             warmup=args.warmup_proportion,
                             t_total=t_total)

    # ==== TRAIN ====
    global_step = 0
    nb_tr_steps = 0
    tr_loss = 0
    if args.do_train:
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_steps)

        model.train()
        for _ in trange(int(args.num_train_epochs), desc="Epoch"):
            tr_loss = 0
            nb_tr_examples, nb_tr_steps = 0, 0
            train_iterator = tqdm(train_dataloader, desc="Iteration")
            for step, batch in enumerate(train_iterator):
                batch = tuple(t.to(device) for t in batch)
                input_ids, input_mask, segment_ids, label_ids = batch
                loss = model(input_ids, segment_ids, input_mask, label_ids)
                if n_gpu > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                if args.fp16:
                    optimizer.backward(loss)
                else:
                    loss.backward()

                tr_loss += loss.item()
                nb_tr_examples += input_ids.size(0)
                nb_tr_steps += 1
                if (step + 1) % args.gradient_accumulation_steps == 0:
                    # modify learning rate with special warm up BERT uses
                    lr_this_step = args.learning_rate * \
                        warmup_linear(global_step/t_total,
                                      args.warmup_proportion)
                    for param_group in optimizer.param_groups:
                        param_group['lr'] = lr_this_step
                    optimizer.step()
                    optimizer.zero_grad()
                    global_step += 1
        print()
    # Save train loss
    result = {"global_step": global_step,
              "loss": tr_loss/nb_tr_steps if args.do_train else None}

    # Save a trained model
    model_to_save = model.module if hasattr(
        model, "module") else model  # Only save the model it-self
    output_model_file = os.path.join(args.output_dir, "pytorch_model.bin")
    if args.do_train:
        torch.save(model_to_save.state_dict(), output_model_file)

    # Load a trained model that you have fine-tuned
    model_state_dict = torch.load(output_model_file)
    if not args.dry_run:
        model = BertForSequenceClassification.from_pretrained(
            args.bert_model,
            state_dict=model_state_dict,
            num_labels=num_labels
        )
        model.to(device)

    # Prepare data
    if args.do_eval or (args.do_prune and args.eval_pruned):
        if args.dry_run:
            eval_examples = processor.get_dummy_dev_examples(args.data_dir)
        else:
            eval_examples = processor.get_dev_examples(args.data_dir)
        # data.add_dependency_arcs(eval_examples)
        # print(eval_examples[-2].parse_a)
        eval_data = data.prepare_tensor_dataset(
            eval_examples,
            label_list,
            args.max_seq_length,
            tokenizer,
            verbose=args.verbose,
        )

    is_main = args.local_rank == -1 or torch.distributed.get_rank() == 0

    # Parse pruning descriptor
    to_prune = pruning.parse_head_pruning_descriptors(
        args.attention_mask_heads,
        reverse_descriptors=args.reverse_head_mask,
    )
    # Mask heads
    model.bert.mask_heads(to_prune)

    # ==== PRUNE ====
    if args.do_prune and is_main:
        if args.fp16:
            raise NotImplementedError("FP16 is not yet supported for pruning")

        # Determine the number of heads to prune
        prune_sequence = pruning.determine_pruning_sequence(
            args.prune_number,
            args.prune_percent,
            model.bert.config.num_hidden_layers,
            model.bert.config.num_attention_heads,
            args.at_least_one_head_per_layer,
        )

        # TODO: refqctor
        for step, n_to_prune in enumerate(prune_sequence):

            if step == 0 or args.exact_pruning:
                # Calculate importance scores for each layer
                head_importance = calculate_head_importance(
                    model,
                    train_data,
                    batch_size=args.train_batch_size,
                    device=device,
                    normalize_scores_by_layer=args.normalize_pruning_by_layer,
                    subset_size=args.compute_head_importance_on_subset,
                    verbose=False,
                )

                print("Head importance scores")
                for layer in range(len(head_importance)):
                    layer_importance = head_importance[layer].cpu().data
                    print("\t".join(f"{x:.5f}" for x in layer_importance))
            # Determine which heads to prune
            to_prune = pruning.what_to_prune(
                head_importance,
                n_to_prune,
                to_prune=to_prune,
                at_least_one_head_per_layer=args.at_least_one_head_per_layer
            )
            # Actually mask the heads
            model.bert.mask_heads(to_prune)
            # Evaluate
            if args.eval_pruned:
                # Print the pruning descriptor
                print("Evaluating following pruning strategy")
                print(pruning.to_pruning_descriptor(to_prune))
                # Eval accuracy
                accuracy = evaluate(
                    eval_data,
                    model,
                    args.eval_batch_size,
                    save_attention_probs=args.save_attention_probs,
                    print_head_entropy=True,
                    device=device,
                    verbose=False,
                )["eval_accuracy"]
                logger.info("***** Pruning eval results *****")
                tot_pruned = sum(len(heads) for heads in to_prune.values())
                print(f"{tot_pruned}\t{accuracy}")

    # ==== EVALUATE ====
    if args.do_eval and is_main:
        evaluate(
            eval_data,
            model,
            args.eval_batch_size,
            save_attention_probs=args.save_attention_probs,
            print_head_entropy=True,
            device=device,
            result=result
        )
        output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
        with open(output_eval_file, "w") as writer:
            logger.info("***** Eval results *****")
            for key in sorted(result.keys()):
                logger.info("  %s = %s", key, str(result[key]))
                writer.write("%s = %s\n" % (key, str(result[key])))

    # ==== ANALYZIS ====
    if args.do_anal:
        if not data.is_nli_task(processor):
            logger.warn(
                f"You are running analysis on the NLI diagnostic set but the "
                f"task ({args.task_name}) is not NLI"
            )
        anal_processor = data.DiagnosticProcessor()
        if args.dry_run:
            anal_examples = anal_processor.get_dummy_dev_examples(
                args.anal_data_dir)
        else:
            anal_examples = anal_processor.get_dev_examples(args.anal_data_dir)
        anal_data = data.prepare_tensor_dataset(
            anal_examples,
            label_list,
            args.max_seq_length,
            tokenizer,
            verbose=args.verbose,
        )
        predictions = predict(
            anal_data,
            model,
            args.eval_batch_size,
            verbose=True,
            device=device,
        )
        report = analyze_nli(anal_examples, predictions, label_list)
        # Print report
        for feature, values in report.items():
            print("=" * 80)
            print(f"Scores breakdown for feature: {feature}")
            for value, accuracy in values.items():
                print(f"{value}\t{accuracy:.5f}")


if __name__ == "__main__":
    main()

