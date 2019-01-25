
import argparse


def get_base_parser():
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument(
        "--data_dir",
        default=None,
        type=str,
        required=True,
        help="The input data dir. Should contain the .tsv files (or other "
        "data files) for the task."
    )
    parser.add_argument(
        "--bert_model", default=None, type=str, required=True,
        help="Bert pre-trained model selected in the list: bert-base-uncased, "
        "bert-large-uncased, bert-base-cased, bert-large-cased, "
        "bert-base-multilingual-uncased, bert-base-multilingual-cased, "
        "bert-base-chinese."
    )
    parser.add_argument(
        "--task_name",
        default=None,
        type=str,
        required=True,
        help="The name of the task to train."
    )
    parser.add_argument(
        "--output_dir",
        default=None,
        type=str,
        required=True,
        help="The output directory where the model "
        "predictions and checkpoints will be written."
    )

    # Other parameters
    parser.add_argument(
        "--max_seq_length",
        default=128,
        type=int,
        help="The maximum total input sequence length after "
        "WordPiece tokenization. \nSequences longer than this"
        " will be truncated, and sequences shorter \n"
        "than this will be padded."
    )
    parser.add_argument(
        "--dry_run",
        action='store_true',
        help="Run all steps with a small model and sample data."
    )
    parser.add_argument(
        "--do_train",
        action='store_true',
        help="Whether to run training."
    )
    parser.add_argument(
        "--do_prune",
        action='store_true',
        help="Whether to run pruning."
    )
    parser.add_argument(
        "--do_eval",
        action='store_true',
        help="Whether to run eval on the dev set."
    )
    parser.add_argument(
        "--do_lower_case",
        action='store_true',
        help="Set this flag if you are using an uncased model."
    )
    parser.add_argument(
        "--no_cuda",
        action='store_true',
        help="Whether not to use CUDA when available"
    )
    parser.add_argument(
        '--seed',
        type=int,
        default=42,
        help="random seed for initialization"
    )
    return parser


def training_args(parser):
    train_group = parser.add_argument_group("Training")
    train_group.add_argument(
        "--train_batch_size",
        default=32,
        type=int,
        help="Total batch size for training."
    )
    train_group.add_argument(
        "--learning_rate",
        default=5e-5,
        type=float,
        help="The initial learning rate for Adam."
    )
    train_group.add_argument(
        "--num_train_epochs",
        default=3.0,
        type=float,
        help="Total number of training epochs to perform."
    )
    train_group.add_argument(
        "--warmup_proportion",
        default=0.1,
        type=float,
        help="Proportion of training to perform linear "
        "learning rate warmup for. "
        "E.g., 0.1 = 10%% of training."
    )
    train_group.add_argument(
        "--local_rank",
        type=int,
        default=-1,
        help="local_rank for distributed training on gpus"
    )
    train_group.add_argument(
        '--gradient_accumulation_steps',
        type=int,
        default=1,
        help="Number of updates steps to accumulate before "
        "performing a backward/update pass."
    )


def pruning_args(parser):
    prune_group = parser.add_argument_group("Pruning")
    prune_group.add_argument(
        "--compute_head_importance_on_subset",
        default=1.0,
        type=float,
        help="Percentage of the training data to use for estimating "
        "head importance."
    )
    prune_group.add_argument(
        "--prune_percent",
        default=[50],
        type=float,
        nargs="*",
        help="Percentage of heads to prune."
    )
    prune_group.add_argument(
        "--prune_number",
        default=None,
        nargs="*",
        type=int,
        help="Number of heads to prune. Overrides `--prune_percent`"
    )
    prune_group.add_argument(
        "--normalize_pruning_by_layer",
        action='store_true',
        help="Normalize importance score by layers for pruning"
    )
    prune_group.add_argument(
        "--at_least_one_head_per_layer",
        action='store_true',
        help="Keep at least one attention head per layer"
    )
    prune_group.add_argument(
        "--eval_pruned",
        action='store_true',
        help="Evaluate the network after pruning"
    )


def eval_args(parser):
    eval_group = parser.add_argument_group("Evaluation")
    eval_group.add_argument(
        "--eval_batch_size",
        default=8,
        type=int,
        help="Total batch size for eval."
    )
    eval_group.add_argument(
        "--attention-mask-heads", default="", type=str, nargs="*",
        help="[layer]:[head1],[head2]..."
    )
    eval_group.add_argument(
        '--reverse-head-mask',
        action='store_true',
        help="Mask all heads except those specified by "
        "`--attention-mask-heads`"
    )
    eval_group.add_argument(
        '--save-attention-probs', default="", type=str,
        help="Save attention to file"
    )


def fp16_args(parser):
    fp16_group = parser.add_argument_group("FP16")
    fp16_group.add_argument(
        '--fp16',
        action='store_true',
        help="Whether to use 16-bit float precision instead of"
        " 32-bit"
    )
    fp16_group.add_argument(
        '--loss_scale',
        type=float, default=0,
        help="Loss scaling to improve fp16 numeric stability. "
        "Only used when fp16 set to True.\n"
        "0 (default value): dynamic loss scaling.\n"
        "Positive power of 2: static loss scaling value.\n"
    )
