# Copyright 2020 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""FFCC TensorFlow ops."""
import math
import sys

import numpy as np
import tensorflow.compat.v1 as tf


def r2c(real):
  """Real to complex."""
  return tf.complex(real, tf.zeros_like(real))


def c2r(comp):
  """Complex to real."""
  return tf.real(comp)


def r2c_fft2(x):
  """Apply 2D-FFT over a real multi-dimensional tensor.

  Args:
    x: A tensor in real number. The rank of the tensor is == 4.

  Returns:
    A complex tensor where [-1, :, :, i] = fft2(t[-1, :, :, i]).
  """
  ndims = x.get_shape().ndims
  if ndims != 4:
    raise ValueError('Expecting ndims == 4, actual={}'.format(ndims))
  x_fft = tf.transpose(
      tf.signal.fft2d(tf.transpose(r2c(x), [0, 3, 1, 2])), [0, 2, 3, 1])
  return x_fft


def c2r_ifft2(x_fft):
  """Apply 2D-iFFT over a 3D complex tensor and saves only real numbers.

  Args:
    x_fft: A tensor in complex number. The rank of the tensor is == 4.

  Returns:
    A real tensor where [-1, :, :, i] = real(ifft2(t[-1, :, :, i])).
  """
  ndims = x_fft.get_shape().ndims
  if ndims != 4:
    raise ValueError('Expecting ndims == 4, actual={}'.format(ndims))
  return c2r(
      tf.transpose(
          tf.signal.ifft2d(tf.transpose(x_fft, [0, 3, 1, 2])), [0, 2, 3, 1]))


def data_preprocess(rgb, extended_feature, params):
  """Convert inputs to histogram features for TensorFlow.

  This functions preprocesses the input prior to the TensorFlow graph.

  Args:
    rgb: RGB image (float32) with the shape of [batch_size, height, width,
      channels].
    extended_feature: a feature (float32) of the input data, in the shape
      [batch_size, extended_vector_length]
    params: a dict with keys:
      'first_bin': (float) location of the edge of the first histogram bin.
      'bin_size': (float) size of each histogram bin.
      'nbins': (int) number of histogram bins.

  Returns:
    chroma_histograms: stack of 2D chroma histograms (float32) from the
      filter bank. For each channel, the chroma histogram is generated from the
      input as described below:
        ch = 0: from RGB input.
        ch = 1: from edge filter input.
    extended_features: A 1D vector (float32) with encoded extended feature
      bucket weights.
  """
  # TODO(fbleibel): implement this function
  print(rgb, extended_feature, params)
  return None


def eval_features(features, filters_fft, bias):
  """Convolve the features with a 2D-FFT and sum the result across channels.

  This is also known as a convolution layer, where the innermost dimension is
  "batch size", and the outermost dimension is the channels (same behavior as
  tf.conv2d). The operation would return a filtered histogram:
    H = sum(conv2d(features, ifft(filters_fft), boundary='wrap'), axis=3) + bias

  Args:
    features: input multi-channel feature with shape of [batch_size, hight,
      width, channels]
    filters_fft: The fft of input convolution kernels with shape of [batch_size,
      height, width, channels]
    bias: input bias with shape of [batch_size, height, width]

  Returns:
    A convolved result with shape of [batch_size, H, W]
  """
  # Checking shapes
  feature_shape = tf.shape(features)
  batch_size = feature_shape[0]
  height = feature_shape[1]
  width = feature_shape[2]
  num_channels = feature_shape[3]

  deps = [
      tf.assert_equal(batch_size,
                      tf.shape(filters_fft)[0]),
      tf.assert_equal(height,
                      tf.shape(filters_fft)[1]),
      tf.assert_equal(width,
                      tf.shape(filters_fft)[2]),
      tf.assert_equal(num_channels,
                      tf.shape(filters_fft)[3]),
      tf.assert_equal(batch_size,
                      tf.shape(bias)[0]),
      tf.assert_equal(height,
                      tf.shape(bias)[1]),
      tf.assert_equal(width,
                      tf.shape(bias)[2]),
  ]
  with tf.control_dependencies(deps):
    fx_fft = tf.reduce_sum(
        r2c_fft2(features) * filters_fft, axis=3, keepdims=True)
    fx = c2r_ifft2(fx_fft)

    # Squeeze the last dimension from [batch, n, n, 1] to [batch, n , n]
    h = tf.add(tf.squeeze(fx, axis=[3]), bias, name='H')
    return h


def softmax2(h):
  """Applies a softmax function produced a normalized Probability Mass Function.

  Args:
    h: input tensor as shape of [batch_size, H, W]

  Returns:
    output PMF (Probability Mass Function) with shape [batch_size, H, W].
  """
  # Find the max value from h.
  ndims = h.get_shape().ndims
  if ndims != 3:
    raise ValueError('Expecting ndims = 3, actual={}'.format(ndims))
  _, height, width = h.get_shape().as_list()
  return tf.reshape(
      tf.nn.softmax(tf.reshape(h, [-1, width * height])), [-1, height, width])


def bivariate_von_mises(pmf):
  """Approximately fits a bivariate von Mises over a PMF.

  Given a 2D PDF histogram (PMF), approximately fits a bivariate von Mises
  distribution to that PDF by computing the local moments. This produces a
  center of mass of the PDF, where the PDF is assumed to lie on a torus rather
  than a cartesian space.

  Args:
    pmf: a 2D PDF histogram with the shape of [batch_size, H, W]. The sum of a
      pmf should be 1.

  Returns:
    Tuple of:
      mu: the center mass of the 2D PDF histogram in index space (0-base), with
        shape of [batch_size, 2].
      sigma: the isotropic co-variance matrix in index space (0-base), with
        shape of [batch_size, 2, 2].
  """
  # The PMF is in the shape of [-1, V, U]
  pmf_shape = pmf.get_shape().as_list()
  ndims = pmf.get_shape().ndims
  sums = tf.reduce_sum(pmf, axis=list(range(1, ndims)), keepdims=True)
  deps = [
      # Expect 3-channel input
      tf.assert_equal(ndims, 3),
      # Expect the shape is a square
      tf.assert_equal(pmf_shape[1], pmf_shape[2]),
      # Expect the sum of PMF is 1
      tf.assert_near(sums, 1, atol=1e-4)
  ]

  with tf.control_dependencies(deps):
    sum_u = tf.reduce_sum(pmf, axis=2)
    sum_v = tf.reduce_sum(pmf, axis=1)

    size = pmf_shape[1]
    angle_step = 2. * math.pi / size
    angles = tf.reshape(
        tf.range(size, dtype=tf.float32) * angle_step, [1, size])

    cos_angles = tf.cos(angles)
    sin_angles = tf.sin(angles)

    # Compute the expected value of sine and cosine to handle wrap-around
    # boundary
    expected_cos_v = tf.reduce_sum(sum_v * cos_angles, axis=1, keepdims=True)
    expected_sin_v = tf.reduce_sum(sum_v * sin_angles, axis=1, keepdims=True)
    expected_cos_u = tf.reduce_sum(sum_u * cos_angles, axis=1, keepdims=True)
    expected_sin_u = tf.reduce_sum(sum_u * sin_angles, axis=1, keepdims=True)

    # With the expected cosine and sine of the angle, we can compute the angular
    # center of mass by computing the arctangent.
    # Note: atan2 returns the range of [-PI .. PI], and we want to shift it to
    # [0 .. 2*PI] and snap into histogram grids
    # NOTE:
    #   There is a TF bug that causing tf.mod returns incorrect result when the
    #   concatenated input is given:
    #     theta = tf.atan2(expected_sin_u, expected_cos_u)
    #     phi = tf.atan2(expected_sin_v, expected_cos_v)
    #     mean_angle = tf.mod(tf.concat([theta, phi], axis=1), 2.0 * math.pi)
    #   phi will return incorrect value:
    #     2.71980858 % (2.0 * PI) becomes 2.76635933.
    # The following attempt is to avoid the TF bug.
    two_pi = 2.0 * math.pi
    theta = tf.mod(tf.atan2(expected_sin_u, expected_cos_u), two_pi)
    phi = tf.mod(tf.atan2(expected_sin_v, expected_cos_v), two_pi)
    mean_angle = tf.concat([theta, phi], axis=1)

    # Convert the angle back to histogram grid indices
    mu = tf.divide(mean_angle, angle_step, name='mu_idx')

    # Compute the covariance matrix.
    bins = tf.range(pmf_shape[1], dtype=tf.float32)
    u_delta = bins - mu[:, tf.newaxis, 0]
    v_delta = bins - mu[:, tf.newaxis, 1]
    wrap = lambda x: tf.mod(x + pmf_shape[1] / 2, pmf_shape[1])
    u_wrapped = wrap(u_delta)
    v_wrapped = wrap(v_delta)
    sum1 = lambda x: tf.reduce_sum(x, axis=-1)
    u_expectation = sum1(sum_u * u_wrapped)
    v_expectation = sum1(sum_v * v_wrapped)
    u_var = sum1(sum_u * u_wrapped**2) - u_expectation**2
    v_var = sum1(sum_v * v_wrapped**2) - v_expectation**2
    uv_expectation = tf.linalg.matvec(
        tf.linalg.matvec(pmf, u_wrapped, transpose_a=True)[..., tf.newaxis, :],
        v_wrapped)[..., 0]
    uv_covar = uv_expectation - u_expectation * v_expectation

    # Construct covariance matrices.
    sigma = tf.reshape(
        tf.stack([u_var, uv_covar, uv_covar, v_var], axis=1), [-1, 2, 2],
        name='sigma_idx')
    return mu, sigma


def idx_to_uv(mu_idx, sigma_idx, step_size, offset):
  """Converts the integer index space back to the UV space.

  Args:
    mu_idx: the index of the center mass of the PMF in the shape of [batch_size,
      2], a TF tensor.
    sigma_idx: the index of the covariance matrix of the PMF in the shape of
      [batch_size, 2, 2], a TF tensor.
    step_size: the pitch of each step, scalar.
    offset: the value of the first index, scalar.

  Returns:
    Tuple of:
      mu: the center mass of the PMF in UV space.
      sigma: the covariance matrix of the PMF In UV space.
  """
  mu_idx.shape.assert_has_rank(2)
  mu_idx.shape.assert_is_compatible_with([None, 2])
  sigma_idx.shape.assert_has_rank(3)
  sigma_idx.shape.assert_is_compatible_with([None, 2, 2])

  mu = tf.add(
      tf.multiply(tf.cast(mu_idx, dtype=tf.float32), step_size),
      offset,
      name='mu')
  sigma = tf.multiply(
      tf.cast(sigma_idx, dtype=tf.float32), step_size**2, name='sigma')
  return mu, sigma


def splat_non_uniform(x, bins):
  """Non-uniformly splats a input scalar over a 1D grid.

  Linearly interpolate into the bins using x, where the bins have non-uniform
  spacing. The values in the bins must be sorted from small to large. This
  method takes NumPy arrays instead of TF tensors, and returns a Numpy array.

  Args:
    x: A 1D vector of values being splatted, in the shape of [batch_size].
    bins: 1D vector, in the shape of [N], where N is the number of bins.

  Returns:
    A 2D matrix with interpolated weights in the shape of [batch_size, M], where
    each row is the splat weights.
  """

  x = np.asarray(x)
  bins = np.asarray(bins)

  # clamps the x into the boundary of bins
  if (bins.ndim != 1 or x.ndim != 1 or bins.shape[0] <= 1 or x.shape[0] < 1):
    raise ValueError('Input and output parameters does not meet requirement: '
                     'x.shape={}, bin.shape={}'.format(x.shape, bins.shape))

  if bins.shape[0] == 1:
    return np.ones((x.shape[0], 1))

  clamped_x = np.clip(x, bins[0], bins[-1])
  delta_x = clamped_x[:, np.newaxis] - bins
  idx_lo = np.minimum(
      delta_x.shape[1] - np.argmax(delta_x[:, ::-1] >= 0., axis=1) - 1,
      bins.shape[0] - 2)
  idx_hi = idx_lo + 1
  w_hi = (clamped_x - bins[idx_lo]) / (bins[idx_hi] - bins[idx_lo])
  w_lo = 1 - w_hi

  f = np.zeros((x.shape[0], bins.shape[0]))
  f[range(x.shape[0]), idx_lo] = w_lo
  f[range(x.shape[0]), idx_hi] = w_hi

  return f


def uv_to_pmf(uv, step_size, offset, n):
  """Returns bilinearly-interpolated PMFs from each given log-UV coordinates.

  Args:
    uv: float, the log-UV coordinates that represent ground truth labels in the
      shape of [batch_size, 2]. The uv needs to be within the range of [offset,
      offset + step_size * (n-1)].
    step_size: float, the pitch of each step, scalar.
    offset: float, the value of the first index, scalar.
    n: float, the number of bins, scalar.

  Returns:
    float, Bilinearly-interpolated PMF in the shape of [batch_size, n, n].
  """

  uv = tf.convert_to_tensor(uv)

  # If UV values are not within the PMF, we emit a warning and clamp them.
  # TODO(barron): Investigate why this ever happens in the training code.
  uv_min = offset
  uv_max = offset + (n - 1) * step_size

  def uv_fmin():
    return tf.Print(uv, [
        'WARNING: uv_to_pmf() given values of ',
        tf.reduce_min(uv), ' < ', uv_min, ', clipping.'
    ])

  def uv_fmax():
    return tf.Print(uv, [
        'WARNING: uv_to_pmf() given values of ',
        tf.reduce_max(uv), ' > ', uv_max, ', clipping.'
    ])

  uv = tf.cond(tf.reduce_any(uv < uv_min), uv_fmin, lambda: uv)
  uv = tf.cond(tf.reduce_any(uv > uv_max), uv_fmax, lambda: uv)

  uv = tf.clip_by_value(uv, offset, uv_max)

  uv.shape.assert_is_compatible_with([None, 2])
  if not np.isscalar(step_size):
    raise ValueError('`step_size` must be a scalar, but is of type {}'.format(
        type(step_size)))
  if not np.isscalar(offset):
    raise ValueError('`step_size` must be a scalar, but is of type {}'.format(
        type(offset)))

  uv_idx = (uv - offset) / step_size

  uv_idx_lo = tf.floor(uv_idx)
  # Protects from the boundary error by wrapping around. Clamping would cause
  # assertion on tf.SparseTensor due to repeated indices. However, we also
  # checks the range of uv_idx to be within [0 .. n-1], so the clamping would
  # not affect the numerical correctness.
  uv_idx_hi = tf.mod(uv_idx_lo + 1, n)

  w_1 = uv_idx - uv_idx_lo
  w_0 = 1.0 - w_1
  w_00 = w_0[:, 0] * w_0[:, 1]
  w_01 = w_0[:, 0] * w_1[:, 1]
  w_10 = w_1[:, 0] * w_0[:, 1]
  w_11 = w_1[:, 0] * w_1[:, 1]

  uv_idx_lo = tf.cast(uv_idx_lo, dtype=tf.int64)
  uv_idx_hi = tf.cast(uv_idx_hi, dtype=tf.int64)
  batch_size = tf.shape(uv)[0]
  batch_idx = tf.cast(tf.range(batch_size), dtype=tf.int64)
  idx_00 = tf.stack([batch_idx, uv_idx_lo[:, 0], uv_idx_lo[:, 1]], axis=1)
  idx_01 = tf.stack([batch_idx, uv_idx_lo[:, 0], uv_idx_hi[:, 1]], axis=1)
  idx_10 = tf.stack([batch_idx, uv_idx_hi[:, 0], uv_idx_lo[:, 1]], axis=1)
  idx_11 = tf.stack([batch_idx, uv_idx_hi[:, 0], uv_idx_hi[:, 1]], axis=1)

  sparse = tf.SparseTensor(
      indices=tf.concat([idx_00, idx_01, idx_10, idx_11], axis=0),
      values=tf.concat([w_00, w_01, w_10, w_11], axis=0),
      dense_shape=[batch_size, n, n])
  return tf.sparse.to_dense(tf.sparse_reorder(sparse))


def rgb_to_uv(rgb):
  """Converts RGB to log-UV space.

  Args:
    rgb: float, the RGB values that represents the color of illuminants in the
      shape of [batch_size, 3]. The values of RGB has to be >= 0.

  Returns:
    float, the log-UV coordinates that represent white points in the shape of
      [batch_size, 2].
  """

  rgb = tf.convert_to_tensor(rgb)
  rgb.shape.assert_is_compatible_with([None, 3])

  deps = [tf.assert_greater_equal(rgb, tf.cast(0.0, dtype=rgb.dtype))]
  # Protects the value from division by 0
  rgb = tf.maximum(rgb, sys.float_info.epsilon)
  with tf.control_dependencies(deps):
    log_rgb = tf.log(rgb)
    # u = log(g/r), v = log(g/b)
    u = log_rgb[:, 1] - log_rgb[:, 0]
    v = log_rgb[:, 1] - log_rgb[:, 2]
    return tf.stack([u, v], axis=1)


def uv_to_rgb(uv):
  """Converts log-UV to (normalized) RGB space.

  Args:
    uv: float, the log-UV coordinates that represent white points in the shape
      of [batch_size, 2].

  Returns:
    float, normalized RGB values (unit vectors) in the shape of [batch_size, 3].
  """

  uv = tf.convert_to_tensor(uv)
  uv.shape.assert_is_compatible_with([None, 2])

  # u = log(g/r), v = log(g/b)
  rb = tf.exp(-uv)
  rgb = tf.stack(
      [rb[:, 0],
       tf.ones(shape=(tf.shape(rb)[0]), dtype=uv.dtype), rb[:, 1]],
      axis=1)

  return rgb / tf.norm(rgb, axis=1, keepdims=True)


def apply_wb(rgb, uv):
  """Apply white balance gains to a RGB image.

  Args:
    rgb: float, the RGB images in the shape of [batch_size, height, width,
      channel].
    uv: float, white point in log-UV coordinate in the shape of [batch_size, 2].

  Returns:
    float, white balanced RGB images in the shape of
    [batch_size, height, width, channel].
  """
  uv = tf.convert_to_tensor(uv)
  uv.shape.assert_is_compatible_with([None, 2])

  rgb = tf.convert_to_tensor(rgb)
  rgb.shape.assert_is_compatible_with([None, None, None, 3])

  rb_gains = tf.exp(uv)
  wb_gains = tf.stack([
      rb_gains[:, 0],
      tf.ones(shape=tf.shape(rb_gains)[0], dtype=rb_gains.dtype), rb_gains[:, 1]
  ],
                      axis=1)

  return rgb * wb_gains[:, tf.newaxis, tf.newaxis, :]
