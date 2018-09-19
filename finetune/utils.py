import os
import re
from functools import partial
import warnings
import copy

import pandas as pd
import numpy as np
import tensorflow as tf
from tensorflow.python.framework import function
from tensorflow.contrib.crf import viterbi_decode
from tqdm import tqdm
from scipy import interpolate
from sklearn.utils import shuffle

from finetune.encoding import NLP, TextEncoder
from finetune import config

ENCODER = TextEncoder()

def merge_leading_dims(X, target_rank):
    shape = [-1] + X.get_shape().as_list()[1 - target_rank:]
    return tf.reshape(X, shape)


def interpolate_pos_embed(positional_embed, new_len):
    xx = np.linspace(0, 512, new_len)
    newKernel = interpolate.RectBivariateSpline(np.arange(positional_embed.shape[0]),
                                                np.arange(positional_embed.shape[1]), positional_embed)
    return newKernel(xx, np.arange(positional_embed.shape[1]))


def concat_or_stack(tensors, axis=0):
    try:
        return tf.concat(tensors, axis=axis)
    except ValueError:
        # tensors are scalars
        return tf.stack(tensors, axis=axis)


def shuffle_data(*args):
    """
    Thin passthrough fn to sklearn.utils.shuffle, but allows for passing through None values
    """
    shuffled = shuffle(arg for arg in args if arg is not None)
    results = []
    idx = 0
    for arg in args:
        if arg is None:
            results.append(arg)
        else:
            results.append(shuffled[idx])
            idx += 1
    return tuple(results)


def format_gpu_string(num):
    return '/device:GPU:{}'.format(num)


def shape_list(x):
    """
    deal with dynamic shape in tensorflow cleanly
    """
    ps = x.get_shape().as_list()
    ts = tf.shape(x)
    return [ts[i] if ps[i] is None else ps[i] for i in range(len(ps))]


def np_softmax(x, t=1, axis=-1):
    x = x / t
    x = x - np.max(x, axis=axis, keepdims=True)
    ex = np.exp(x)
    return ex / np.sum(ex, axis=axis, keepdims=True)


def make_path(f):
    d = os.path.dirname(f)
    if d and not os.path.exists(d):
        os.makedirs(d)
    return f


def _identity_init(shape, dtype, partition_info, scale):
    n = shape[-1]
    w = np.eye(n) * scale
    if len([s for s in shape if s != 1]) == 2:
        w = w.reshape(shape)
    return w.astype(np.float32)


def identity_init(scale=1.0):
    return partial(_identity_init, scale=scale)


def _np_init(shape, dtype, partition_info, w):
    return w


def np_init(w):
    return partial(_np_init, w=w)


def find_trainable_variables(key, exclude=None):
    """
    Simple helper function to get trainable variables that contain a certain string in their name :param key:, whilst
    excluding variables whos full name (including scope) includes a substring :param excludes:.
    """
    trainable_variables = tf.get_collection(tf.GraphKeys.TRAINABLE_VARIABLES, ".*{}.*".format(key))
    if exclude is not None:
        trainable_variables = [
            var for var in trainable_variables
            if not exclude in var.name
        ]
    return trainable_variables


def soft_split(*xs, n_splits=None):
    """
    Similar to tf.split but can accommodate batches that are not evenly divisible by n_splits.

    Useful for data parallelism across multiple devices, where the batch size is not necessarily divisible by the
    number of devices or is variable between batches.
    """
    if not n_splits or not isinstance(n_splits, int):
        raise ValueError("n_splits must be a valid integer.")

    x = xs[0]
    current_batch_size = shape_list(x)[0]
    n_per = tf.to_int32(tf.ceil(current_batch_size / n_splits))
    for i in range(n_splits):
        start = tf.minimum(i * n_per, current_batch_size)
        end = tf.minimum((i + 1) * n_per, current_batch_size)
        i_range = tf.range(start, end)
        yield [tf.gather(x, i_range) for x in xs]


def flatten(outer):
    return [el for inner in outer for el in inner]


def remove_none(l):
    return [e for e in l if e is not None]


def list_transpose(l):
    return [list(i) for i in zip(*l)]


def iter_data(*datas, n_batch=128, truncate=False, verbose=False, max_batches=float("inf"), tqdm_desc=None):
    n = len(datas[0])
    if truncate:
        n = (n // n_batch) * n_batch
    n = min(n, max_batches * n_batch)
    n_batches = 0

    for i in tqdm(
            range(0, n, n_batch), total=n // n_batch, ncols=80, leave=False, disable=(not verbose),
            desc=tqdm_desc
        ):
        if n_batches >= max_batches: raise StopIteration
        if len(datas) == 1:
            yield datas[0][i:i + n_batch]
        else:
            yield (d[i:i + n_batch] for d in datas)
        n_batches += 1


@function.Defun(
    python_grad_func=lambda x, dy: tf.convert_to_tensor(dy),
    shape_func=lambda op: [op.inputs[0].get_shape()])
def convert_gradient_to_tensor(x):
    """
    force gradient to be a dense tensor
    it's often faster to do dense embedding gradient on GPU than sparse on CPU
    """
    return x


def assign_to_gpu(gpu=0, params_device="/device:CPU:0"):
    """
        A device assignment function to place all variables on :param params_device: and everything else on gpu
        number :param gpu:

        Useful for data parallelism across multiple GPUs.
    """

    def _assign(op):
        node_def = op if isinstance(op, tf.NodeDef) else op.node_def
        if node_def.op == "Variable":
            return params_device
        else:
            return "/gpu:%d" % gpu

    return _assign


def sample_with_temperature(logits, temperature):
    """Either argmax or random sampling.
    Args:
      logits: a Tensor.
      temperature: a float  0.0=argmax 1.0=random
    Returns:
      a Tensor with one fewer dimension than logits.
    """
    if temperature == 0.0:
        # TF argmax doesn't handle >5 dimensions, so we reshape here.
        logits_shape = shape_list(logits)
        argmax = tf.argmax(tf.reshape(logits, [-1, logits_shape[-1]]), axis=1)
        return tf.reshape(argmax, logits_shape[:-1])
    else:
        assert temperature > 0.0
        reshaped_logits = (
                tf.reshape(logits, [-1, shape_list(logits)[-1]]) / temperature)
        choices = tf.multinomial(reshaped_logits, 1)
        choices = tf.reshape(choices,
                             shape_list(logits)[:logits.get_shape().ndims - 1])
        return choices


def average_grads(tower_grads):
    def average_dense(grad_and_vars):
        if len(grad_and_vars) == 1:
            return grad_and_vars[0][0]

        grad = grad_and_vars[0][0]
        for g, _ in grad_and_vars[1:]:
            grad += g
        return grad / len(grad_and_vars)

    def average_sparse(grad_and_vars):
        if len(grad_and_vars) == 1:
            return grad_and_vars[0][0]

        indices = []
        values = []
        for g, _ in grad_and_vars:
            indices += [g.indices]
            values += [g.values]
        indices = tf.concat(indices, 0)
        values = tf.concat(values, 0)
        return tf.IndexedSlices(values, indices, grad_and_vars[0][0].dense_shape)

    average_grads = []
    for grad_and_vars in zip(*tower_grads):
        if grad_and_vars[0][0] is None:
            grad = None
        elif isinstance(grad_and_vars[0][0], tf.IndexedSlices):
            grad = average_sparse(grad_and_vars)
        else:
            grad = average_dense(grad_and_vars)
        v = grad_and_vars[0][1]
        grad_and_var = (grad, v)
        average_grads.append(grad_and_var)
    return average_grads


def truncate_text(text, max_chars=100):
    if len(text) > max_chars:
        text = text[:max_chars] + "..."
    return text

def is_iob_tagged(label):
    return label.startswith("IE-")


def iob_precedes(left, right, pad_token=config.PAD_TOKEN):
    """
    Returns Whether they can be joined, and the new label to give the pair or the right value this defines the policy for which labeling takes place.

    The basic labeling policy, is that once in a sequence, they are given the label equivalent to the value(s) they are looking to be joined with.
    """
    if left == right == pad_token:
        return True, pad_token

    mapping = {
        "B": "IE-",
        "I": "IE-",
        "E": "",
    }
    try:

        right_loc, right_label = right.split("-", 1)

        if left is None or "-" not in left:
            return False, mapping[right_loc] + right_label

        left_loc, left_label = left.split("-", 1)

        if left_label != right_label:
            return False, mapping[right_loc] + right_label

        if right_label in left_label:
            return True, mapping[right_loc] + right_label
    except ValueError:
        return False, right


def finetune_to_indico_sequence(raw_texts, subseqs, labels, probs=None, none_value=config.PAD_TOKEN,
                                subtoken_predictions=False, iob=False):
    """
    Maps from the labeled substring format into the 'indico' format. This is the exact inverse operation to
    :meth indico_to_finetune_sequence:.

    The indico format is as follows:
        Raw text for X,
        Labels as a list of dicts, with each dict in the form:
        {
            'start': <Character index of the start of the labeled sequence>,
            'end': <Character index of the end of the labeled sequence>,
            'label': <A categorical label (int or string) that represents the category of the subsequence,
            'text': <Optionally, a field with the subsequence contained between the start and end.
        }

    The Labeled substring, or finetune internal, format is as follows.
    Each item of the data is a list strings of the form:
        ["The quick brown", "fox", "jumped over the lazy", ...]
    With the corresponding labels:
        ["PAD", "animal", "PAD", ...]

    It is the :param none_value: that is used to populate the PAD labels.
    :param data: A list of segmented text of the form list(list(str))
    :param labels: Categorical labels for each sub-string in data.
    :param none_value: The none value used to encode the input format.
    :return: Texts, annoatations both in the 'indico' format.
    """
    annotations = []
    for raw_text, doc_seq, label_seq, prob_seq in zip(raw_texts, subseqs, labels, probs or [None] * len(raw_texts)):
        tokens = NLP(raw_text)
        token_starts = [token.idx for token in tokens]
        token_ends = [token.idx + len(token.text) for token in tokens]
        n_tokens = len(tokens)

        doc_annotations = []
        annotation_ranges = set()
        start_idx = 0
        end_idx = 0
        raw_annotation_start = 0
        for sub_str, raw_label, confidences in zip(doc_seq, label_seq, prob_seq or [None] * len(doc_seq)):
            if not isinstance(raw_label, tuple):
                multi_label = False
                label_list = [raw_label]
            else:
                multi_label = True
                label_list = raw_label

            for label in label_list:
                new_label = None

                stripped_text = sub_str.strip()


                raw_annotation_start = raw_text.find(stripped_text, raw_annotation_start)
                raw_annotation_end = raw_annotation_start + len(stripped_text)

                if label != none_value:
                    for i, item in enumerate(doc_annotations):
                        if iob:
                            join, new_label = iob_precedes(item["label"], label, none_value)
                        else:
                            join, new_label = item["label"] == label, label
                        if join and raw_annotation_start - item["end"] <= 1:
                            raw_annotation_start = item["start"]
                            doc_annotations.pop(i)
                            break
                if iob and len(doc_annotations) == 0:
                    _, new_label = iob_precedes(None, label, none_value)


                if raw_annotation_start == -1:
                    warnings.warn("Failed to find predicted sequence in text: {}.".format(
                        truncate_text(stripped_text)
                    ))
                    continue

                annotation_start = raw_annotation_start
                annotation_end = raw_annotation_end

                # if we don't want to allow subtoken predictions, adjust start and end to match
                # the start and ends of the nearest full tokens
                if (not subtoken_predictions) and (not iob or not is_iob_tagged(new_label or label)):
                    if multi_label:
                        start_idx = 0
                        end_idx = 0
                    if label != none_value:
                        # round to nearest token
                        while start_idx < n_tokens and annotation_start >= token_starts[start_idx]:
                            start_idx += 1
                        annotation_start = token_starts[start_idx - 1]
                        while end_idx < (n_tokens - 1) and annotation_end > token_ends[end_idx]:
                            end_idx += 1
                        annotation_end = token_ends[end_idx]

                text = raw_text[annotation_start:annotation_end]

                if label != none_value:
                    annotation = {
                        "start": annotation_start,
                        "end": annotation_end,
                        "label": new_label or label,
                        "text": text
                    }
                    if confidences is not None:
                        annotation["confidence"] = confidences

                    # prevent duplicate annotation edge case
                    if (annotation_start, annotation_end, label) not in annotation_ranges:
                        annotation_ranges.add((annotation_start, annotation_end, label))
                        doc_annotations.append(annotation)

        if iob:
            for doc in doc_annotations:
                if is_iob_tagged(doc["label"]):
                    doc["label"] = doc["label"].split("-", 1)[-1]


        doc_annotations = sorted([dict(items) for items in doc_annotations], key=lambda x: x['start'])
        annotations.append(doc_annotations)
    return raw_texts, annotations


def indico_to_finetune_sequence(texts, labels=None, multi_label=True, none_value=config.PAD_TOKEN, subtoken_labels=False, iob=False):
    """
    Maps from the 'indico' format sequence labeling data. Into a labeled substring format. This is the exact inverse of
    :meth finetune_to_indico_sequence:.

    The indico format is as follows:
        Raw text for X,
        Labels as a list of dicts, with each dict in the form:
         labeled sequence>,
            'end': <Character index of the end of the labeled sequence>,
            'label': <A categorical label (int or string) that represents the category of the subsequence,
            'text': <A field containing the sub-sequence contained between the start and end.
        }

    The Labeled substring, or finetune internal, format is as follows.
    Each item of the data is a list strings of the form:{
            'start': <Character index of the start of the
        ["The quick brown", "fox", "jumped over the lazy", ...]
    With the corresponding labels:
        ["PAD", "animal", "PAD", ...]

    It is the :param none_value: that is used to populate the PAD labels.

    :param texts: A list of raw text.
    :param labels: A list of targets of the form list(list(dict))).
    :param none_value: A categorical label to use as the none value.
    :return: Segmented Text, Labels of the form described above.
    """
    all_subseqs = []
    all_labels = []

    # placeholder for inference time
    if labels is None:
        labels = [[]] * len(texts)

    for text, label_seq in zip(texts, labels):
        tokens = NLP(text)
        token_starts = [token.idx for token in tokens]
        token_ends = [token.idx + len(token.text) for token in tokens]
        if iob:
            new_label_seq = []
            for annotation in label_seq:
                start = annotation["start"]
                end = annotation["end"]
                label = annotation["label"]
                if label == none_value:
                    new_label_seq.append(annotation)
                    continue
                assert isinstance(label, str),"to use iob encoding it is necessary to have string labels"
                sub_text = annotation.get("text", None) or text[start:end]

                encoded = ENCODER._encode([sub_text])
                start_char_locs = [0] + encoded.char_locs[0][:-1]

                if len(start_char_locs) > 0:
                    if len(start_char_locs) == 1:
                        B_locs = (start_char_locs[0], len(sub_text))
                    else:
                        B_locs = (start_char_locs[0], start_char_locs[1])
                    new_label_seq.append(
                        dict(
                            start=start + B_locs[0],
                            end=start + B_locs[1],
                            text=sub_text[B_locs[0]:B_locs[1]],
                            label="B-"+label,
                        )
                    )

                if len(start_char_locs) > 1:
                    E_locs = (start_char_locs[-1], len(sub_text))
                    new_label_seq.append(
                        dict(
                            start=start + E_locs[0],
                            end=start + E_locs[1],
                            text=sub_text[E_locs[0]:E_locs[1]],
                            label="E-" + label,
                        )
                    )
                if len(start_char_locs) > 2:
                    new_label_seq.append(
                        dict(
                            start=start + B_locs[1],
                            end=start + E_locs[0],
                            text=sub_text[B_locs[1]:E_locs[0]],
                            label="I-" + label,
                        )
                    )

            label_seq = new_label_seq
        n_tokens = len(tokens)

        label_seq = sorted(label_seq, key=lambda x: x["start"])
        last_loc = 0
        doc_subseqs = []
        doc_labels = []
        for i, annotation in enumerate(label_seq):
            start = annotation["start"]
            end = annotation["end"]
            label = annotation["label"]
            annotation_text = annotation.get("text")
            if not subtoken_labels and not iob:
                if label != none_value:
                    # round to nearest token
                    while start > 0 and start not in token_starts:
                        start -= 1
                    while end < len(text) and end not in token_ends:
                        end += 1

            if start > last_loc:
                doc_subseqs.append(text[last_loc:start])
                if multi_label:
                    doc_labels.append([none_value])
                else:
                    doc_labels.append(none_value)

            j = len(doc_labels) - 1
            split_dist = last_loc - end
            skip_end = 0
            if split_dist > 0:
                j -= 1
                if len(doc_subseqs[-1]) != split_dist:
                    dual_label_sub_seq = doc_subseqs[-1][-split_dist:]
                    doc_subseqs[-1] = doc_subseqs[-1][:-split_dist]
                    doc_subseqs.append(dual_label_sub_seq)
                    doc_labels.append(doc_labels[-1][:])
                    j -= 1

                skip_end = len(doc_subseqs[-1])

            if start < last_loc - skip_end:
                if not multi_label:
                    raise ValueError("Overlapping annotations requires the multi-label model")
                else:
                    split_dist = last_loc - start - skip_end
                    while split_dist >= len(doc_subseqs[j]):
                        doc_labels[j].append(label)
                        split_dist -= len(doc_subseqs[j])
                        j -= 1

                    if split_dist > 0:
                        dual_label_sub_seq = doc_subseqs[j][-split_dist:]
                        doc_subseqs[j] = doc_subseqs[j][:-split_dist]
                        doc_subseqs.insert(j + 1, dual_label_sub_seq)
                        doc_labels.insert(j + 1, doc_labels[j][:] + [label])

                    start = last_loc
                    annotation_text = annotation_text[last_loc - end:]

            if start >= end:
                # degenerate label
                last_loc = max(start, end)
                continue

            doc_subseqs.append(text[start:end])
            if multi_label:
                doc_labels.append([label])
            else:
                doc_labels.append(label)

            if not iob and annotation_text is not None and text[start:end] != annotation_text:
                raise ValueError(
                    "Annotation text does not match text specified by `start` and `end` indexes. "
                    "Text provided: `{}`.  Text extracted: `{}`.".format(
                        annotation_text,
                        text[start:end]
                    )
                )
            last_loc = end

        if last_loc != len(text):
            doc_subseqs.append(text[last_loc:])
            if multi_label:
                doc_labels.append([none_value])
            else:
                doc_labels.append(none_value)
        all_subseqs.append(doc_subseqs)
        all_labels.append(doc_labels)
    return all_subseqs, all_labels
