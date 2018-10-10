import math
import logging

import tqdm

import tensorflow as tf
from tensorflow.python.training import training

LOGGER = logging.getLogger("finetune")


class ProgressHook(training.SessionRunHook):
  
    def __init__(self, n_batches, n_epochs=None):
        self.iterations = 0
        self.n_epochs = n_epochs
        if self.n_epochs:
            self.batches_per_epoch = int(math.ceil(n_batches / n_epochs))
        else:
            self.batches_per_epoch = n_batches
        self.progress_bar = None

    def epoch_descr(self, current_epoch):
        return "Epoch {}/{}".format(current_epoch, self.n_epochs)
    
    def after_run(self, run_context, run_values):
        self.iterations += 1
        current_epoch = self.iterations // self.batches_per_epoch
        current_batch = self.iterations % self.batches_per_epoch

        if self.progress_bar is None:
            self.progress_bar = tqdm.tqdm(total=self.batches_per_epoch)

        if self.n_epochs:
            self.progress_bar.set_description(self.epoch_descr(current_epoch))

        self.progress_bar.n = current_batch
        self.progress_bar.refresh()

    def end(self, session):
        LOGGER.info("Training complete.")
        del self.progress_bar


class PatchedParameterServerStrategy(tf.contrib.distribute.ParameterServerStrategy):

    def _verify_destinations_not_different_worker(self, *args, **kwargs):
        # this is currently broken in tf 1.11.0 -- mock this for now
        pass


class LazySummaryHook(tf.train.SummarySaverHook):
    def __init__(self, save_steps=None,
                 save_secs=None,
                 output_dir=None,
                 summary_writer=None):
        super().__init__(save_steps=save_steps, save_secs=save_secs, output_dir=output_dir,
                         summary_writer=summary_writer, scaffold=1)  # scaffold = 1 suppresses exception in __init__

    def _get_summary_op(self):
        """Fetches the summary op either from self._summary_op or self._scaffold.

        Returns:
          Returns a list of summary `Tensor`.
        """
        if self._summary_op is not None:
            summary_op = self._summary_op
        else:
            summary_op = tf.train.Scaffold.get_or_default('summary_op',
                                                           tf.GraphKeys.SUMMARY_OP,
                                                           tf.summary.merge_all)
        if not isinstance(summary_op, list):
            return [summary_op]
        return summary_op
