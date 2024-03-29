# Code referenced from https://gist.github.com/gyglim/1f8dfb1b5c82627ae3efcfbbadb9f514

import numpy as np
import scipy.misc
import tensorflow as tf
from torch.utils.tensorboard import SummaryWriter
import torch
try:
    from StringIO import StringIO  # Python 2.7
except ImportError:
    from io import BytesIO  # Python 3.x


class Logger(object):

    def __init__(self, log_dir):
        """Create a summary writer logging to log_dir."""
        self.writer = tf.summary.create_file_writer(log_dir)

    def scalar_summary(self, tag, value, step):
        """Log a scalar variable."""
        if isinstance(value, torch.Tensor):
            value = value.cpu().numpy()
        with self.writer.as_default():
            tf.summary.scalar(tag, value, step=step)
            self.writer.flush()

    def image_summary(self, tag, images, step):
        """Log a list of images."""
        with self.writer.as_default():
            for i, img in enumerate(images):
                rgb_image_uint8 = tf.constant([
                    img,
                ], dtype=tf.uint8)
                tf.summary.image(f"{tag}/{i}", rgb_image_uint8, step=step)
            self.writer.flush()

    def histo_summary(self, tag, values, step, bins=1000):
        """Log a histogram of the tensor of values."""

        with self.writer.as_default():
            tf.summary.histogram(tag, values, step=step, buckets=bins)
            self.writer.flush()
