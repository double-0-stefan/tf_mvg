import tensorflow as tf
from mvg_distributions import covariance_representations as cov_rep
import numpy as np
#from tensorflow_probability.python.distributions import seed_stream

import tensorflow_probability as tfp
#from util import SeedStream


class CholeskyWishart(tfp.distributions.Distribution):
    """
    If a p-dimensional vector x follows a multivariate Normal distribution x ~ N(mu, S)
    The inverse covariance S^{-1} = L follows a Wishart distribution L ~ WI(df, Scale)
    The matrices from the Cholesky decomposition L = M M^T follow a Cholesky-Wishart distribution
        M ~ Chol-WI(df, Scale)

    The probability density function (pdf) of the Chol-WI is given by a transformation of the Wishart distribution pdf
        pdf(M) = WI(L; df, Scale) J, where J = 2^p prod_{j=0}^{p-1} M[j,j]^{p-j}.

    The pdf of a Wishart distribution is given by
      WI(X; df, Scale) = det(X)**(0.5 (df-p-1)) exp(-0.5 tr[inv(Scale) X]) / Z
      Z = 2**(0.5 df p) |det(Scale)|**(0.5 df) Gamma_k(0.5 df)

      where:

      * `df >= p` denotes the degrees of freedom,
      * `Scale` is a symmetric, positive definite, `p x p` matrix,
      * `Z` is the normalizing constant, and,
      * `Gamma_k` is the [multivariate Gamma function](
        https://en.wikipedia.org/wiki/Multivariate_gamma_function).

    This implementation of the distribution is only defined for diagonal matrices S.
    Moreover it's optimized for sparse Cholesky matrices as used in the PrecisionConvCholFilters class.

    Example use:

        b = 5  # Batch size
        iw = 4  # Image width
        p = iw * iw  # Number of pixels
        nb = 2  # Number of basis
        kw = 3  # Kernel width

        x_weights = tf.abs(tf.random_normal(shape=(b, iw, iw, nb)))
        x_basis = tf.abs(tf.random_normal(shape=(nb, kw, kw, 1, 1)))
        sample_shape = (b, iw, iw, 1)

        x_cov_obj = PrecisionConvCholFilters(weights_precision=x_weights, filters_precision=x_basis,
                                             sample_shape=sample_shape)

        df = tf.random_uniform(shape=(b,), minval=p, maxval=p * 10)
        log_diag_scale = tf.random_normal(shape=(b, p))

        sqrt_w_dist = SqrtWishart(log_diag_scale=log_diag_scale, df=df)

        # Efficient use
        log_prob1 = sqrt_w_dist.log_prob(x_cov_obj)

        # Equivalent inefficient use
        log_prob2 = sqrt_w_dist.log_prob(x_cov_obj.chol_precision)

    """

    def __init__(self, df, log_diag_scale, add_sparsity_correction=False, add_mode_correction=False,
                 validate_args=False, allow_nan_stats=True, name="SqrtWishart"):
        """

        Args:
            The distribution is defined for batch (b) of M (pxp) matrices, forming a tensor of [b, p, p]

            df: degrees of freedom, a tensor of [b], the values in it must be df > p - 1
            log_diag_scale: a tensor of [b, p] with the log diagonal values of the matrix S
            add_sparsity_correction: bool, if True removes from the likelihood the sparse terms in L
            add_mode_correction: bool, if using the distribution as a prior, setting this to True will add
                a correction factor to log_diag_scale, such that the log_prob will have the maximum in S
            validate_args:
            allow_nan_stats:
            name:
        """
        parameters = locals()

        with tf.name_scope(name=name):
            df = tf.convert_to_tensor(df)
            log_diag_scale = tf.convert_to_tensor(log_diag_scale)

            assert df.shape.ndims == 1
            assert log_diag_scale.shape.ndims == 2

        self._df = df
        self._log_diag_scale = log_diag_scale
        self.add_sparsity_correction = add_sparsity_correction
        graph_parents = [df, log_diag_scale]

        self.p = self.log_diag_scale.shape[1].value
        if self.p is None:
            self.p = tf.shape(self.log_diag_scale)[1]

        self._mode_correction_factor(add_mode_correction)

        super().__init__(dtype=self.df.dtype, reparameterization_type=tf.distributions.FULLY_REPARAMETERIZED,
                         validate_args=validate_args, allow_nan_stats=allow_nan_stats, parameters=parameters,
                         graph_parents=graph_parents, name=name)

    @property
    def df(self):
        """Cholesky-Wishart distribution degree(s) of freedom."""
        return self._df

    @property
    def log_diag_scale(self):
        """Cholesky-Wishart distribution log diagonal of the scale matrix."""
        return self._log_diag_scale

    def _mode_correction_factor(self, add_mode_correction):
        if add_mode_correction:
            # corrected_diag_scale =  diag_scale * ((p - 1)/(tf.range(p) * (1 - p) + (p - 1) * (df - 1)))
            correction_factor = tf.log(self.p - 1.)
            p_range = tf.range(self.p, dtype=self._log_diag_scale.dtype)[tf.newaxis, :]
            correction_factor -= tf.log(p_range * (1. - self.p) + (self.p - 1.) * (self.df[:, tf.newaxis] - 1.))
            self._log_diag_scale += correction_factor

    def _batch_shape_tensor(self):
        return tf.shape(self.log_diag_scale)[0]

    def _batch_shape(self):
        return self.log_diag_scale.shape[0:1]

    def _event_shape_tensor(self):
        event_dim = tf.shape(self.log_diag_scale)[1]
        return tf.stack([event_dim, event_dim])

    def _event_shape(self):
        event_dim = self.log_diag_scale.shape[1]
        return tf.TensorShape([event_dim, event_dim])

    def log_normalization(self, name="log_normalization"):
        """Computes the log normalizing constant, log(Z)."""
        with self._name_scope(name):
            log_det_scale = 0.5 * tf.reduce_sum(self.log_diag_scale, axis=1)

            return (self.df * log_det_scale +
                    0.5 * self.df * self.p * tf.log(2.) +
                    self._multi_lgamma(0.5 * self.df, self.p))

    def _multi_gamma_sequence(self, a, p, name="multi_gamma_sequence"):
        """Creates sequence used in multivariate (di)gamma; shape = shape(a)+[p]."""
        with self._name_scope(name, values=[a, p]):
            # Linspace only takes scalars, so we'll add in the offset afterwards.
            seq = tf.linspace(
                tf.constant(0., dtype=self.dtype),
                0.5 - 0.5 * p,
                tf.cast(p, tf.int32))
            return seq + tf.expand_dims(a, [-1])

    def _multi_lgamma(self, a, p, name="multi_lgamma"):
        """Computes the log multivariate gamma function; log(Gamma_p(a))."""
        with self._name_scope(name, values=[a, p]):
            seq = self._multi_gamma_sequence(a, p)
            return (0.25 * p * (p - 1.) * tf.log(np.pi) +
                    tf.reduce_sum(tf.lgamma(seq),
                                  axis=[-1]))

    def _multi_digamma(self, a, p, name="multi_digamma"):
        """Computes the multivariate digamma function; Psi_p(a)."""
        with self._name_scope(name, values=[a, p]):
            seq = self._multi_gamma_sequence(a, p)
            return tf.reduce_sum(tf.digamma(seq),
                                 axis=[-1])

    @staticmethod
    def _convert_to_cov_obj(value):
        if not isinstance(value, cov_rep.PrecisionConvCholFilters):
            value = tf.convert_to_tensor(value, name="value")
            log_prob_shape = ()
            if value.shape.ndims == 2:
                # Add batch dimension
                value = tf.expand_dims(value, axis=0)
            if value.shape.ndims == 3:
                log_prob_shape = tf.shape(value)[0:1]
            if value.shape.ndims == 4:
                # Collapse batch and sample dimension
                shape = tf.shape(value)
                log_prob_shape = shape[0:2]
                new_shape = [log_prob_shape[0] * log_prob_shape[1]]
                new_shape = tf.concat((new_shape, shape[2:]), axis=0)
                value = tf.reshape(value, new_shape)
            value = cov_rep.PrecisionCholesky(chol_precision=value)
        else:
            log_prob_shape = value.sample_shape[0:1]

        return value, log_prob_shape

    def _call_log_prob(self, value, name, **kwargs):
        with self._name_scope(name, values=[value]):
            value, log_prob_shape = self._convert_to_cov_obj(value)
            try:
                log_prob = self._log_prob(value)
                return tf.reshape(log_prob, log_prob_shape)
            except NotImplementedError as original_exception:
                try:
                    log_prob = tf.log(self._prob(value))
                    return tf.reshape(log_prob, log_prob_shape)
                except NotImplementedError:
                    raise original_exception

    def _log_det_jacobian(self, x):
        # Create a vector equal to: [p, p-1, ..., 2, 1].
        if isinstance(self.p, tf.Tensor):
            p_float = tf.cast(self.p, dtype=x.dtype)
        else:
            p_float = np.array(self.p, dtype=x.dtype.as_numpy_dtype)
        exponents = tf.linspace(p_float, 1., self.p)
        exponents = tf.expand_dims(exponents, axis=0)  # Add batch dim

        if hasattr(x, 'log_diag_chol_precision'):
            log_diag_chol_precision = x.log_diag_chol_precision
        else:
            log_diag_chol_precision = tf.log(tf.matrix_diag_part(x.chol_precision))

        # Log determinant of the Jacobian
        ldj = tf.reduce_sum(log_diag_chol_precision * exponents, axis=1)
        ldj += p_float * tf.log(2.)
        return ldj

    def _log_unnormalized_prob(self, x):
        diag_inv_scale = tf.exp(-self.log_diag_scale)

        # Log determinant of the precision matrix
        log_det_precision = -x.log_det_covariance()

        # trace( inv(Scale) @ Precision )
        trace_scale_inv_x = tf.reduce_sum(diag_inv_scale * x.precision_diag_part, axis=1)

        # Un-normalized Wishart log probability
        return (self.df - self.p - 1) * 0.5 * log_det_precision - 0.5 * trace_scale_inv_x

    @staticmethod
    def _get_num_sparse_per_row(x):
        """
        Get an array with the number of sparse elements per row. The upper triangular elements are not considered
          sparse for this function, the diagonal is not sparse either.

        :param x: a PrecisionConvCholFilters instance
        :return: ndarray of size [n], where n is the number of pixels
        """
        n = x.recons_filters_precision.shape[1].value

        np_off_diag_mask = x.np_off_diag_mask()

        sparse_per_row = np.sum(1 - np_off_diag_mask, axis=1)  # Sum over columns to get number per row

        # Number of sparse elements per row by construction due to the the matrix being upper triangular
        upper_triangular = np.arange(n, 0, -1)
        sparse_per_row -= upper_triangular  # Do not take into account the upper triangular part
        return np.maximum(sparse_per_row, 0)

    def _log_sparsity_correction(self, x):
        # The sparsity correction factor evaluates N(0, v) for all sparse off-diagonal elements
        error_msg = "Sparsity correction is only supported for PrecisionConvCholFilters"
        assert isinstance(x, cov_rep.PrecisionConvCholFilters), error_msg

        num_sparse_per_row = self._get_num_sparse_per_row(x)
        num_sparse_per_row = num_sparse_per_row[np.newaxis, :]  # Add batch dim

        sqrt_scale = tf.exp(0.5 * self.log_diag_scale)

        # The sparse elements are zero, so it's log prob of 0 with size [batch, n]
        normal_dist = tf.distributions.Normal(loc=0.0, scale=sqrt_scale)
        zero_prob = normal_dist.log_prob(0.0)

        # Each ith row has its own v_i, so for each row the probability is evaluated according to v_i
        # for the the number of sparse elements in the row
        log_prob = zero_prob * num_sparse_per_row

        # Sum over pixels, so that output is size [b]
        return tf.reduce_sum(log_prob, axis=1)

    def _log_prob(self, x):
        log_prob = self._log_unnormalized_prob(x) + self._log_det_jacobian(x) - self.log_normalization()
        if self.add_sparsity_correction:
            log_prob -= self._log_sparsity_correction(x)
        return log_prob

    def _sample_n(self, n, seed=None):
        # This implementation is equivalent to the one in tf.contrib.distributions.Wishart
        batch_shape = self.batch_shape_tensor()
        event_shape = self.event_shape_tensor()

        stream = tfp.util.SeedStream(seed=seed, salt="Wishart")

        shape = tf.concat([[n], batch_shape, event_shape], 0)

        # Sample a normal full matrix
        x = tf.random_normal(shape=shape, dtype=self.dtype, seed=stream())

        # Sample the diagonal
        g = tf.random_gamma(shape=[n], alpha=self._multi_gamma_sequence(0.5 * self.df, self.p), beta=0.5,
                            dtype=self.dtype, seed=stream())

        # Discard the upper triangular part
        x = tf.matrix_band_part(x, -1, 0)

        # Set the diagonal
        x = tf.matrix_set_diag(x, tf.sqrt(g))

        # Scale with the Scale matrix, equivalent to matmul(sqrt(diag_scale), x)
        x *= tf.sqrt(tf.exp(self.log_diag_scale[tf.newaxis, :, :, tf.newaxis]))

        return x

    def _sample_n_sparse(self, n, kw, seed=None):
        assert n == 1

        batch_shape = self.batch_shape_tensor()
        event_shape = self.event_shape
        iw = int(np.sqrt(event_shape[0].value))  # Image width
        nb = kw ** 2  # Number of basis
        nb_half = nb // 2 + 1
        nch = 1  # Number of channels in the image

        stream = tfp.util.SeedStream(seed=seed, salt="Wishart")

        shape = tf.concat([batch_shape, [iw, iw, nb_half - 1]], 0)

        # Random sample for the off diagonal values as a dense tensor
        x_right = tf.random_normal(shape=shape, dtype=self.dtype, seed=stream())

        # The upper triangular values needed to get a square kernel per pixel
        x_left = tf.zeros(shape)

        # Random sample for the diagonal of the matrix
        x_diag = tf.random_gamma(shape=[n], alpha=self._multi_gamma_sequence(0.5 * self.df, self.p), beta=0.5,
                                 dtype=self.dtype, seed=stream())

        # Concatenate the diagonal and off-diagonal elements
        x_diag = tf.reshape(x_diag, (-1, iw, iw, nch))
        x = tf.concat([tf.sqrt(x_diag), x_right], axis=3)

        # Scale the sampled matrix using the distribution Scale matrix
        diag_scale = tf.exp(self.log_diag_scale)
        diag_scale = tf.reshape(diag_scale, (-1, iw, iw, nch))
        x *= tf.sqrt(diag_scale)  # Square root is equivalent to Cholesky

        # Concatenate with the zeros
        x = tf.concat([x_left, x], axis=3)

        # Create identity basis so that the sampled matrix is only defined by x, if this were not the case
        # we would have to do some optimization to find the basis and weights that reconstruct x
        identity_basis = tf.eye(num_rows=nb)
        identity_basis = tf.reshape(identity_basis, (nb, kw, kw, nch, nch))

        sample_shape = tf.concat([batch_shape, [iw, iw, nch]], axis=0)
        x_sparse = cov_rep.PrecisionConvCholFilters(weights_precision=x, filters_precision=identity_basis,
                                                    sample_shape=sample_shape)

        return x_sparse

    def sample_sparse(self, kw, sample_shape=(), seed=None, name="sample"):
        # This method produces biased samples, as the off-diagonal elements not in the neighbour of kw
        # for each pixel are set to zero, as they will not be represented in the output
        with self._name_scope(name, values=[sample_shape]):
            sample_shape = tf.convert_to_tensor(sample_shape, dtype=tf.int32, name="sample_shape")
            sample_shape, n = self._expand_sample_shape_to_vector(sample_shape, "sample_shape")
            return self._sample_n_sparse(n, kw, seed)


class Wishart(CholeskyWishart):
    """ A Wishart distribution that can be evaluated using a covariance matrix parametrised as
        PrecisionConvCholFilters """

    def _log_prob(self, x):
        log_prob = self._log_unnormalized_prob(x) - self.log_normalization()
        if self.add_sparsity_correction:
            raise NotImplementedError("")
            # Not sure if the sparsity correction is the same as the Cholesky-Wishart or not
            # log_prob -= self._log_sparsity_correction(x)
        return log_prob

    def _sample_n(self, n, seed=None):
        chol_x = super()._sample_n(n, seed=seed)
        return tf.matmul(chol_x, chol_x, transpose_b=True)
