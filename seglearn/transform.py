'''
This module is for transforming time series data.
'''
# Authors: David Burns, Matthias Gazzari, Philip Boyer
# License: BSD

import numpy as np
from scipy.interpolate import interp1d
from sklearn.base import BaseEstimator, TransformerMixin
from sklearn.utils import check_random_state, check_array, check_consistent_length, shuffle
from sklearn.utils.fixes import signature
from sklearn.exceptions import NotFittedError
from sklearn.utils import check_random_state, check_array, check_consistent_length
from sklearn.utils.metaestimators import _BaseComposition

from .base import TS_Data
from .feature_functions import base_features
from .util import get_ts_data_parts, check_ts_data

__all__ = ['SegmentX', 'SegmentXY', 'SegmentXYForecast', 'PadTrunc', 'InterpLongToWide', 'Interp',
           'FeatureRep', 'FeatureRepMix', 'FunctionTransformer', 'patch_sampler']


class XyTransformerMixin(object):
    ''' Base class for transformer that transforms data and target '''

    def fit_transform(self, X, y, sample_weight=None, **fit_params):
        '''
        Fit the data and transform (required by sklearn API)

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
           Time series data and (optionally) contextual data
        y : array-like shape [n_series], default = None
            target vector
        sample_weight : array-like shape [n_series], default = None
            sample weights

        Returns
        -------
        X_new : array-like, shape [n_segments, ]
            transformed time series data
        y_new : array-like, shape [n_segments]
            expanded target vector
        sample_weight_new : array-like shape [n_segments]
            expanded sample weights
        '''
        return self.fit(X, y, **fit_params).transform(X, y, sample_weight)


def last(y):
    ''' Returns the last column from 2d matrix '''
    return y[:, (y.shape[1] - 1)]


def middle(y):
    ''' Returns the middle column from 2d matrix '''
    return y[:, y.shape[1] // 2]


def mean(y):
    ''' returns average along axis 1'''
    return np.mean(y, axis=1)


def every(y):
    ''' Returns all values (sequences) of y '''
    return y


def shuffle_data(X, y=None, sample_weight=None):
    ''' Shuffles indices X, y, and sample_weight together'''
    if len(X) > 1:
        ind = np.arange(len(X), dtype=np.int)
        np.random.shuffle(ind)
        Xt = X[ind]
        yt = y
        swt = sample_weight

        if yt is not None:
            yt = yt[ind]
        if swt is not None:
            swt = swt[ind]

        return Xt, yt, swt
    else:
        return X, y, sample_weight


class SegmentX(BaseEstimator, XyTransformerMixin):
    '''
    Transformer for sliding window segmentation for datasets where
    X is time series data, optionally with contextual variables
    and each time series in X has a single target value y

    The target y is mapped to all segments from their parent series.
    The transformed data consists of segment/target pairs that can be learned
    through a feature representation or directly with a neural network.

    Parameters
    ----------
    width : int > 0
        width of segments (number of samples)
    overlap : float range [0,1]
        amount of overlap between segments. must be in range: 0 <= overlap <= 1
        (note: setting overlap to 1.0 results in the segments to being advanced by a single sample)
    step : int range [1, width] (default=None)
        number of samples to advance adjacent segments (note: this takes precedence over overlap)
    shuffle : bool, optional
        shuffle the segments after transform (recommended for batch optimizations)
    random_state : int, default = None
        Randomized segment shuffling will return different results for each call to
        ``transform``. If you have set ``shuffle`` to True and want the same result
        with each call to ``fit``, set ``random_state`` to an integer.
    order : str, optional (default='F')
        Determines the index order of the segmented time series. 'C' means C-like index order (first
        index changes slowest) and 'F' means Fortran-like index order (last index changes slowest).
        'C' ordering is suggested for neural network estimators, and 'F' ordering is suggested for computing
        feature representations.

    Todo
    ----
    separate fit and predict overlap parameters
    '''

    def __init__(self, width=100, overlap=0.5, step=None, shuffle=False, random_state=None,
                 order='F'):
        self.width = width
        self.overlap = overlap if step is None else None
        self.step = step
        self.shuffle = shuffle
        self.random_state = random_state
        self.order = order
        self._validate_params()

    @property
    def _step(self):
        if self.step is not None:
            return self.step
        else:
            return max(1, int(self.width * (1. - self.overlap)))

    def _validate_params(self):
        if not self.width >= 1:
            raise ValueError("width must be >=1 (was %d)" % self.width)
        if self.overlap is not None and not (self.overlap >= 0.0 and self.overlap <= 1.0):
            raise ValueError("overlap must be >=0 and <=1.0 (was %.2f)" % self.overlap)
        if self.step is not None and not (self.step >= 1 and self.step <= self.width):
            raise ValueError('step must be >=1 and <=width=%s (was %s)' % (self.width, self.step))
        if self.overlap is None and self.step is None:
            raise ValueError('Either overlap or step must be set to a valid number')
        if not self.order in ('C', 'F'):
            raise ValueError('order must be either "C" or "F" (was %s' % self.order)

    def fit(self, X, y=None):
        '''
        Fit the transform

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Time series data and (optionally) contextual data
        y : None
            There is no need of a target in a transformer, yet the pipeline API requires
            this parameter.
        shuffle : bool
            Shuffles data after transformation

        Returns
        -------
        self : object
            Returns self.
        '''
        check_ts_data(X, y)
        return self

    def transform(self, X, y=None, sample_weight=None):
        '''
        Transforms the time series data into segments (temporal tensor)
        Note this transformation changes the number of samples in the data
        If y and sample_weight are provided, they are transformed to align to the new samples


        Parameters
        ----------
        X : array-like, shape [n_series, ...]
           Time series data and (optionally) contextual data
        y : array-like shape [n_series], default = None
            target vector
        sample_weight : array-like shape [n_series], default = None
            sample weights

        Returns
        -------
        Xt : array-like, shape [n_segments, ]
            transformed time series data
        yt : array-like, shape [n_segments]
            expanded target vector
        sample_weight_new : array-like shape [n_segments]
            expanded sample weights
        '''
        check_ts_data(X, y)
        Xt, Xc = get_ts_data_parts(X)
        yt = y
        swt = sample_weight

        N = len(Xt)  # number of time series

        if Xt[0].ndim > 1:
            Xt = np.array([sliding_tensor(Xt[i], self.width, self._step, self.order)
                           for i in np.arange(N)])
        else:
            Xt = np.array([sliding_window(Xt[i], self.width, self._step, self.order)
                           for i in np.arange(N)])

        Nt = [len(Xt[i]) for i in np.arange(len(Xt))]
        Xt = np.concatenate(Xt)

        if yt is not None:
            yt = expand_variables_to_segments(yt, Nt).ravel()

        if swt is not None:
            swt = expand_variables_to_segments(swt, Nt).ravel()

        if Xc is not None:
            Xc = expand_variables_to_segments(Xc, Nt)
            Xt = TS_Data(Xt, Xc)

        if self.shuffle is True:
            check_random_state(self.random_state)
            return shuffle_data(Xt, yt, swt)

        return Xt, yt, swt


class SegmentXY(BaseEstimator, XyTransformerMixin):
    '''
    Transformer for sliding window segmentation for datasets where
    X is time series data, optionally with contextual variables
    and y is also time series data with the same sampling interval as X

    The target y is mapped to segments from their parent series,
    using the parameter ``y_func`` to determine the mapping behavior.
    The segment targets can be a single value, or a sequence of values
    depending on ``y_func`` parameter.

    The transformed data consists of segment/target pairs that can be learned
    through a feature representation or directly with a neural network.


    Parameters
    ----------
    width : int > 0
        width of segments (number of samples)
    overlap : float range [0,1]
        amount of overlap between segments. must be in range: 0 <= overlap <= 1
        (note: setting overlap to 1.0 results in the segments to being advanced by a single sample)
    step : int range [1, width] (default=None)
        number of samples to advance adjacent segments (note: this takes precedence over overlap)
    y_func : function
        returns target from array of target segments (eg ``last``, ``middle``, or ``mean``)
    shuffle : bool, optional
        shuffle the segments after transform (recommended for batch optimizations)
    random_state : int, default = None
        Randomized segment shuffling will return different results for each call to ``transform``.
        If you have set ``shuffle`` to True and want the same result with each call to ``fit``,
        set ``random_state`` to an integer.
    order : str, optional (default='F')
        Determines the index order of the segmented time series. 'C' means C-like index order (first
        index changes slowest) and 'F' means Fortran-like index order (last index changes slowest).
        'C' ordering is suggested for neural network estimators, and 'F' ordering is suggested for computing
        feature representations.

    Returns
    -------
    self : object
        Returns self.
    '''

    def __init__(self, width=100, overlap=0.5, step=None, y_func=last, shuffle=False,
                 random_state=None, order='F'):
        self.width = width
        self.overlap = overlap if step is None else None
        self.step = step
        self.y_func = y_func
        self.shuffle = shuffle
        self.random_state = random_state
        self.order = order
        self._validate_params()

    @property
    def _step(self):
        if self.step is not None:
            return self.step
        else:
            return max(1, int(self.width * (1. - self.overlap)))

    def _validate_params(self):
        if not self.width >= 1:
            raise ValueError("width must be >=1 (was %d)" % self.width)
        if self.overlap is not None and not (self.overlap >= 0.0 and self.overlap <= 1.0):
            raise ValueError("overlap must be >=0 and <=1.0 (was %.2f)" % self.overlap)
        if self.step is not None and not (self.step >= 1 and self.step <= self.width):
            raise ValueError('step must be >=1 and <=width=%s (was %s)' % (self.width, self.step))
        if self.overlap is None and self.step is None:
            raise ValueError('Either overlap or step must be set to a valid number')
        if not self.order in ('C', 'F'):
            raise ValueError('order must be either "C" or "F" (was %s' % self.order)

    def fit(self, X, y=None):
        '''
        Fit the transform

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Time series data and (optionally) contextual data
        y : None
            There is no need of a target in a transformer, yet the pipeline API requires this
            parameter.

        Returns
        -------
        self : object
            Returns self.
        '''
        check_ts_data(X, y)
        return self

    def transform(self, X, y=None, sample_weight=None):
        '''
        Transforms the time series data into segments
        Note this transformation changes the number of samples in the data
        If y is provided, it is segmented and transformed to align to the new samples as per
        ``y_func``
        Currently sample weights always returned as None

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
           Time series data and (optionally) contextual data
        y : array-like shape [n_series], default = None
            target vector
        sample_weight : array-like shape [n_series], default = None
            sample weights

        Returns
        -------
        Xt : array-like, shape [n_segments, ]
            transformed time series data
        yt : array-like, shape [n_segments]
            expanded target vector
        sample_weight_new : None

        '''
        check_ts_data(X, y)
        Xt, Xc = get_ts_data_parts(X)
        yt = y

        N = len(Xt)  # number of time series

        if Xt[0].ndim > 1:
            Xt = np.array([sliding_tensor(Xt[i], self.width, self._step, self.order)
                           for i in np.arange(N)])
        else:
            Xt = np.array([sliding_window(Xt[i], self.width, self._step, self.order)
                           for i in np.arange(N)])

        Nt = [len(Xt[i]) for i in np.arange(len(Xt))]
        Xt = np.concatenate(Xt)

        if Xc is not None:
            Xc = expand_variables_to_segments(Xc, Nt)
            Xt = TS_Data(Xt, Xc)

        if yt is not None:
            yt = np.array([sliding_window(yt[i], self.width, self._step, self.order)
                           for i in np.arange(N)])
            yt = np.concatenate(yt)
            yt = self.y_func(yt)

        if self.shuffle is True:
            check_random_state(self.random_state)
            Xt, yt, _ = shuffle_data(Xt, yt)

        return Xt, yt, None


class SegmentXYForecast(BaseEstimator, XyTransformerMixin):
    '''
    Forecast sliding window segmentation for time series or sequence datasets

    The target y is mapped to segments from their parent series,
    using the ``forecast`` and ``y_func`` parameters to determine the mapping behavior.
    The segment targets can be a single value, or a sequence of values
    depending on ``y_func`` parameter.

    The transformed data consists of segment/target pairs that can be learned
    through a feature representation or directly with a neural network.

    Parameters
    ----------
    width : int > 0
        width of segments (number of samples)
    overlap : float range [0,1]
        amount of overlap between segments. must be in range: 0 <= overlap <= 1
        (note: setting overlap to 1.0 results in the segments to being advanced by a single sample)
    step : int range [1, width] (default=None)
        number of samples to advance adjacent segments (note: this takes precedence over overlap)
    forecast : int
        The number of samples ahead in time to forecast
    y_func : function
        returns target from array of target forecast segments (eg ``last``, or ``mean``)
    shuffle : bool, optional
        shuffle the segments after transform (recommended for batch optimizations)
    random_state : int, default = None
        Randomized segment shuffling will return different results for each call to ``transform``.
        If you have set ``shuffle`` to True and want the same result with each call to ``fit``, set
        ``random_state`` to an integer.
    order : str, optional (default='F')
        Determines the index order of the segmented time series. 'C' means C-like index order (first
        index changes slowest) and 'F' means Fortran-like index order (last index changes slowest).
        'C' ordering is suggested for neural network estimators, and 'F' ordering is suggested for computing
        feature representations.

    Returns
    -------
    self : object
        Returns self.
    '''

    def __init__(self, width=100, overlap=0.5, step=None, forecast=10, y_func=last, shuffle=False,
                 random_state=None, order='F'):
        self.width = width
        self.overlap = overlap if step is None else None
        self.step = step
        self.forecast = forecast
        self.y_func = y_func
        self.shuffle = shuffle
        self.random_state = random_state
        self.order = order
        self._validate_params()

    @property
    def _step(self):
        if self.step is not None:
            return self.step
        else:
            return max(1, int(self.width * (1. - self.overlap)))

    def _validate_params(self):
        if not self.width >= 1:
            raise ValueError("width must be >=1 (was %d)" % self.width)
        if self.overlap is not None and not (self.overlap >= 0.0 and self.overlap <= 1.0):
            raise ValueError("overlap must be >=0 and <=1.0 (was %.2f)" % self.overlap)
        if self.step is not None and not (self.step >= 1 and self.step <= self.width):
            raise ValueError('step must be >=1 and <=width=%s (was %s)' % (self.width, self.step))
        if self.overlap is None and self.step is None:
            raise ValueError('Either overlap or step must be set to a valid number')
        if not self.forecast >= 1:
            raise ValueError("forecase must be >=1 (was %d)" % self.forecast)
        if not self.order in ('C', 'F'):
            raise ValueError('order must be either "C" or "F" (was %s' % self.order)

    def fit(self, X=None, y=None):
        '''
        Fit the transform

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Time series data and (optionally) contextual data
        y : None
            There is no need of a target in a transformer, yet the pipeline API requires this
            parameter.

        Returns
        -------
        self : object
            Returns self.
        '''
        check_ts_data(X, y)
        return self

    def transform(self, X, y, sample_weight=None):
        '''
        Forecast sliding window segmentation for time series or sequence datasets.
        Note this transformation changes the number of samples in the data.
        Currently sample weights always returned as None.

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
           Time series data and (optionally) contextual data
        y : array-like shape [n_series]
            target vector
        sample_weight : array-like shape [n_series], default = None
            sample weights

        Returns
        -------
        X_new : array-like, shape [n_segments, ]
            segmented X data
        y_new : array-like, shape [n_segments]
            forecast y data
        sample_weight_new : None

        '''
        check_ts_data(X, y)
        Xt, Xc = get_ts_data_parts(X)
        yt = y

        # if only one time series is learned
        if len(Xt[0]) == 1:
            Xt = [Xt]

        N = len(Xt)  # number of time series

        if Xt[0].ndim > 1:
            Xt = np.array([sliding_tensor(Xt[i], self.width + self.forecast, self._step, self.order)
                           for i in np.arange(N)])
        else:
            Xt = np.array([sliding_window(Xt[i], self.width + self.forecast, self._step, self.order)
                           for i in np.arange(N)])

        Nt = [len(Xt[i]) for i in np.arange(len(Xt))]
        Xt = np.concatenate(Xt)

        # todo: implement advance X
        Xt = Xt[:, 0:self.width]

        if Xc is not None:
            Xc = expand_variables_to_segments(Xc, Nt)
            Xt = TS_Data(Xt, Xc)

        if yt is not None:
            yt = np.array([sliding_window(yt[i], self.width + self.forecast, self._step, self.order)
                           for i in np.arange(N)])
            yt = np.concatenate(yt)
            yt = yt[:, self.width:(self.width + self.forecast)]  # target y
            yt = self.y_func(yt)

        if self.shuffle is True:
            check_random_state(self.random_state)
            Xt, yt, _ = shuffle_data(Xt, yt)

        return Xt, yt, None


def expand_variables_to_segments(v, Nt):
    ''' expands contextual variables v, by repeating each instance as specified in Nt '''
    N_v = len(np.atleast_1d(v[0]))
    return np.concatenate([np.full((Nt[i], N_v), v[i]) for i in np.arange(len(v))])


def sliding_window(time_series, width, step, order='F'):
    '''
    Segments univariate time series with sliding window

    Parameters
    ----------
    time_series : array like shape [n_samples]
        time series or sequence
    width : int > 0
        segment width in samples
    step : int > 0
        stepsize for sliding in samples

    Returns
    -------
    w : array like shape [n_segments, width]
        resampled time series segments
    '''
    w = np.hstack([time_series[i:1 + i - width or None:step] for i in range(0, width)])
    result = w.reshape((int(len(w) / width), width), order='F')
    if order == 'F':
        return result
    else:
        return np.ascontiguousarray(result)


def sliding_tensor(mv_time_series, width, step, order='F'):
    '''
    segments multivariate time series with sliding window

    Parameters
    ----------
    mv_time_series : array like shape [n_samples, n_variables]
        multivariate time series or sequence
    width : int > 0
        segment width in samples
    step : int > 0
        stepsize for sliding in samples

    Returns
    -------
    data : array like shape [n_segments, width, n_variables]
        segmented multivariate time series data
    '''
    D = mv_time_series.shape[1]
    data = [sliding_window(mv_time_series[:, j], width, step, order) for j in range(D)]
    return np.stack(data, axis=2)


class PadTrunc(BaseEstimator, XyTransformerMixin):
    '''
    Transformer for using padding and truncation to enforce fixed length on all time
    series in the dataset. Series' longer than ``width`` are truncated to length ``width``.
    Series' shorter than length ``width`` are padded at the end with zeros up to length ``width``.

    The same behavior is applied to the target if it is a series and passed to the transformer.

    Parameters
    ----------
    width : int >= 1
        width of segments (number of samples)
    '''

    def __init__(self, width=100):
        if not width >= 1:
            raise ValueError("width must be >= 1 (was %d)" % width)
        self.width = width

    def _mv_resize(self, v):
        N = len(v)
        if v[0].ndim > 1:
            D = v[0].shape[1]
            w = np.zeros((N, self.width, D))
        else:
            w = np.zeros((N, self.width))
        for i in np.arange(N):
            Ni = min(self.width, len(v[i]))
            w[i, 0:Ni] = v[i][0:Ni]
        return w

    def fit(self, X, y=None):
        '''
        Fit the transform. Does nothing, for compatibility with sklearn API.

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Time series data and (optionally) contextual data
        y : None
            There is no need of a target in a transformer, yet the pipeline API requires this
            parameter.

        Returns
        -------
        self : object
            Returns self.
        '''
        check_ts_data(X, y)
        return self

    def transform(self, X, y=None, sample_weight=None):
        '''
        Transforms the time series data into fixed length segments using padding and or truncation
        If y is a time series and passed, it will be transformed as well

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
           Time series data and (optionally) contextual data
        y : array-like shape [n_series], default = None
            target vector
        sample_weight : array-like shape [n_series], default = None
            sample weights

        Returns
        -------
        X_new : array-like, shape [n_series, ]
            transformed time series data
        y_new : array-like, shape [n_series]
            expanded target vector
        sample_weight_new : None

        '''
        check_ts_data(X, y)
        Xt, Xc = get_ts_data_parts(X)
        yt = y
        swt = sample_weight

        Xt = self._mv_resize(Xt)

        if Xc is not None:
            Xt = TS_Data(Xt, Xc)

        if yt is not None and len(np.atleast_1d(yt[0])) > 1:
            # y is a time series
            yt = self._mv_resize(yt)
            swt = None
        elif yt is not None:
            # todo: is this needed?
            yt = np.array(yt)

        return Xt, yt, swt


class Interp(BaseEstimator, XyTransformerMixin):
    '''
    Transformer for resampling time series data to a fixed period over closed interval
    (direct value interpolation).
    Default interpolation is linear, but other types can be specified.
    If the target is a series, it will be resampled as well.

    categorical_target should be set to True if the target series is a class
    The transformer will then use nearest neighbor interp on the target.

    This transformer assumes the time dimension is column 0, i.e. X[0][:,0]
    Note the time dimension is removed, since this becomes a linear sequence.
    If start time or similar is important to the estimator, use a context variable.

    Parameters
    ----------
    sample_period : numeric
        desired sampling period
    kind : string
        interpolation type - valid types as per scipy.interpolate.interp1d
    categorical_target : bool
        set to True for classification problems to use nearest instead of linear interp for  the
        target

    '''

    def __init__(self, sample_period, kind='linear', categorical_target=False):
        if not sample_period > 0:
            raise ValueError("sample_period must be >0 (was %f)" % sample_period)

        self.sample_period = sample_period
        self.kind = kind
        self.categorical_target = categorical_target

    def fit(self, X, y=None):
        '''
        Fit the transform. Does nothing, for compatibility with sklearn API.

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Time series data and (optionally) contextual data
        y : None
            There is no need of a target in a transformer, yet the pipeline API requires this
            parameter.

        Returns
        -------
        self : object
            Returns self.
        '''
        check_ts_data(X, y)
        if not X[0].ndim > 1:
            raise ValueError("X variable must have more than 1 channel")

        return self

    def _interp(self, t_new, t, x, kind):
        interpolator = interp1d(t, x, kind=kind, copy=False, bounds_error=False,
                                fill_value="extrapolate", assume_sorted=True)
        return interpolator(t_new)

    def transform(self, X, y=None, sample_weight=None):
        '''
        Transforms the time series data with linear direct value interpolation
        If y is a time series and passed, it will be transformed as well
        The time dimension is removed from the data

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
           Time series data and (optionally) contextual data
        y : array-like shape [n_series], default = None
            target vector
        sample_weight : array-like shape [n_series], default = None
            sample weights

        Returns
        -------
        X_new : array-like, shape [n_series, ]
            transformed time series data
        y_new : array-like, shape [n_series]
            expanded target vector
        sample_weight_new : array-like or None
            None is returned if target is changed. Otherwise it is returned unchanged.
        '''
        check_ts_data(X, y)
        Xt, Xc = get_ts_data_parts(X)
        yt = y
        swt = sample_weight

        N = len(Xt)  # number of series
        D = Xt[0].shape[1] - 1  # number of data channels

        # 1st channel is time
        t = [Xt[i][:, 0] for i in np.arange(N)]
        t_lin = [np.arange(Xt[i][0, 0], Xt[i][-1, 0], self.sample_period) for i in np.arange(N)]

        if D == 1:
            Xt = [self._interp(t_lin[i], t[i], Xt[i][:, 1], kind=self.kind) for i in np.arange(N)]
        elif D > 1:
            Xt = [np.column_stack([self._interp(t_lin[i], t[i], Xt[i][:, j], kind=self.kind)
                                   for j in range(1, D + 1)]) for i in np.arange(N)]
        if Xc is not None:
            Xt = TS_Data(Xt, Xc)

        if yt is not None and len(np.atleast_1d(yt[0])) > 1:
            # y is a time series
            swt = None
            if self.categorical_target is True:
                yt = [self._interp(t_lin[i], t[i], yt[i], kind='nearest') for i in np.arange(N)]
            else:
                yt = [self._interp(t_lin[i], t[i], yt[i], kind=self.kind) for i in np.arange(N)]
        else:
            # y is static - leave y alone
            pass

        return Xt, yt, swt


class InterpLongToWide(BaseEstimator, XyTransformerMixin):
    '''
    Converts time series in long format dataframes (where variables are sampled at different times)
    to wide format data frames usable by the rest of seglearn using direct value interpolation.

    Input data for this class must have at least 3 columns of type (time, var_type, var_value)
    Additional columns are treated as additional channels of var_value
    (e.g. time, var_type, var_value1, var_value2).

    Each time series must have the same var_types and the same number of columns.

    Default interpolation is linear, but other types can be specified.
    If the target is a series, it will be resampled as well.

    categorical_target should be set to True if the target series is a class
    The transformer will then use nearest neighbor interp on the target.

    The interpolation to a linear sampling space, and conversion to wide format dataframe results
    in the removal of the time column and var_type columns in the data.

    If start time or similar is important to the estimator, use a context variable.

    Parameters
    ----------
    sample_period : numeric
        desired sampling period
    kind : string
        interpolation type - valid types as per scipy.interpolate.interp1d
    categorical_target : bool
        set to True for classification problems to use nearest instead of linear interp for  the
        target

    Examples
    --------
    >>> import numpy as np
    >>> from seglearn.transform import InterpLongToWide
    >>>
    >>> # sample stacked input with values from 2 variables each with 2 channels
    >>> t = np.array([1.1, 1.2, 2.1, 3.3, 3.4, 3.5])
    >>> s = np.array([0, 1, 0, 0, 1, 1])
    >>> v1 = np.array([3, 4, 5, 7, 15, 25])
    >>> v2 = np.array([5, 7, 6, 9, 22, 35])
    >>> X = [np.column_stack([t, s, v1, v2])]
    >>> y = [np.array([1, 2, 2, 2, 3, 3])]
    >>>
    >>> stacked_interp = InterpLongToWide(0.5)
    >>> stacked_interp.fit(X, y)
    >>> Xc, yc, _ = stacked_interp.transform(X, y)

    '''

    def __init__(self, sample_period, kind='linear', categorical_target=False):
        if not sample_period > 0:
            raise ValueError("sample_period must be >0 (was %f)" % sample_period)

        self.sample_period = sample_period
        self.kind = kind
        self.categorical_target = categorical_target

    def fit(self, X, y=None):
        '''
        Fit the transform. Does nothing, for compatibility with sklearn API.

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Time series data and (optionally) contextual data
        y : None
            There is no need of a target in a transformer, yet the pipeline API requires this
            parameter.

        Returns
        -------
        self : object
            Returns self.
        '''
        self._check_data(X)
        if not X[0].ndim >= 2:
            raise ValueError("X input must be 2 dim array or greater")
        return self

    def _check_data(self, X):
        '''
        Checks that unique identifiers vaf_types are consistent between time series.

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Time series data and (optionally) contextual data
        '''

        if len(X) > 1:
            sval = np.unique(X[0][:, 1])
            if np.all([np.all(np.unique(X[i][:, 1]) == sval) for i in range(1, len(X))]):
                pass
            else:
                raise ValueError("Unique identifier var_types not consistent between time series")

    def _interp(self, t_new, t, x, kind):
        interpolator = interp1d(t, x, kind=kind, copy=False, bounds_error=False,
                                fill_value="extrapolate", assume_sorted=True)
        return interpolator(t_new)

    def transform(self, X, y=None, sample_weight=None):
        '''
        Transforms the time series data with linear direct value interpolation
        If y is a time series and passed, it will be transformed as well
        The time dimension is removed from the data

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
           Time series data and (optionally) contextual data
        y : array-like shape [n_series], default = None
            target vector
        sample_weight : array-like shape [n_series], default = None
            sample weights

        Returns
        -------
        X_new : array-like, shape [n_series, ]
            transformed time series data
        y_new : array-like, shape [n_series]
            expanded target vector
        sample_weight_new : array-like or None
            None is returned if target is changed. Otherwise it is returned unchanged.
        '''
        check_ts_data(X, y)
        xt, xc = get_ts_data_parts(X)
        yt = y
        swt = sample_weight

        # number of data channels
        d = xt[0][0].shape[0] - 2
        # number of series
        N = len(xt)

        # retrieve the unique identifiers
        s = np.unique(xt[0][:, 1])

        x_new = []
        t_lin = []

        # transform x
        for i in np.arange(N):

            # splits series into a list for each variable
            xs = [xt[i][xt[i][:, 1] == s[j]] for j in np.arange(len(s))]

            # find latest/earliest sample time for each identifier's first/last time sample time
            t_min = np.max([np.min(xs[j][:, 0]) for j in np.arange(len(s))])
            t_max = np.min([np.max(xs[j][:, 0]) for j in np.arange(len(s))])

            # Generate a regular series of timestamps starting at tStart and tEnd for sample_period
            t_lin.append(np.arange(t_min, t_max, self.sample_period))

            # Interpolate for the new regular sample times
            if d == 1:
                x_new.append(
                    np.column_stack(
                        [self._interp(t_lin[i], xs[j][:, 0], xs[j][:, 2], kind=self.kind)
                         for j in np.arange(len(s))]))
            elif d > 1:
                xd = []
                for j in np.arange(len(s)):
                    # stack the columns of each variable by dimension d after interpolation to new regular sample times
                    temp = np.column_stack(
                        [(self._interp(t_lin[i], xs[j][:, 0], xs[j][:, k], kind=self.kind))
                         for k in np.arange(2, 2 + d)])
                    xd.append(temp)
                # column stack each of the sensors s -- resulting in s*d columns
                x_new.append(np.column_stack(xd))

        # transform y
        if yt is not None and len(np.atleast_1d(yt[0])) > 1:
            # y is a time series
            swt = None
            if self.categorical_target is True:
                yt = [self._interp(t_lin[i], xt[i][:, 0], yt[i], kind='nearest') for i in
                      np.arange(N)]
            else:
                yt = [self._interp(t_lin[i], xt[i][:, 0], yt[i], kind=self.kind) for i in
                      np.arange(N)]
        else:
            # y is static - leave y alone
            pass

        if xc is not None:
            x_new = TS_Data(x_new, xc)

        return x_new, yt, swt


class FeatureRep(BaseEstimator, TransformerMixin):
    '''
    A transformer for calculating a feature representation from segmented time series data.

    This transformer calculates features from the segmented time series', by computing the same
    feature set for each segment from each time series in the data set.

    The ``features`` computed are a parameter of this transformer, defined by a dict of functions.
    The seglearn package includes some useful features, but this basic feature set can be easily
    extended.

    Parameters
    ----------
    features : dict, optional
        Dictionary of functions for calculating features from a segmented time series.
        Each function in the dictionary is specified to compute features from a
        multivariate segmented time series along axis 1 (the segment) eg:
            >>> def mean(X):
            >>>    F = np.mean(X, axis = 1)
            >>>    return(F)
            X : array-like shape [n_samples, segment_width, n_variables]
            F : array-like [n_samples, n_features]
            The number of features returned (n_features) must be >= 1

        If features is not specified, a default feature dictionary will be used (see base_features).
        See ``feature_functions`` for example implementations.
    verbose: boolean, optional (default false)
        Controls the verbosity of output messages

    Attributes
    ----------
    f_labels : list of string feature labels (in order) corresponding to the computed features

    Examples
    --------

    >>> from seglearn.transform import FeatureRep, SegmentX
    >>> from seglearn.pipe import Pype
    >>> from seglearn.feature_functions import mean, var, std, skew
    >>> from seglearn.datasets import load_watch
    >>> from sklearn.ensemble import RandomForestClassifier
    >>> data = load_watch()
    >>> X = data['X']
    >>> y = data['y']
    >>> fts = {'mean': mean, 'var': var, 'std': std, 'skew': skew}
    >>> clf = Pype([('seg', SegmentX()),
    >>>             ('ftr', FeatureRep(features = fts)),
    >>>             ('rf',RandomForestClassifier())])
    >>> clf.fit(X, y)
    >>> print(clf.score(X, y))

    '''

    def __init__(self, features='default', verbose=False):
        if features == 'default':
            self.features = base_features()
        else:
            if not isinstance(features, dict):
                raise TypeError("features must either 'default' or an instance of type dict")
            self.features = features

        if type(verbose) != bool:
            raise TypeError("verbose parameter must be type boolean")

        self.verbose = verbose
        self.f_labels = None

    def fit(self, X, y=None):
        '''
        Fit the transform

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Segmented time series data and (optionally) contextual data
        y : None
            There is no need of a target in a transformer, yet the pipeline API requires this
            parameter.

        Returns
        -------
        self : object
            Returns self.
        '''
        check_ts_data(X, y)
        self._reset()
        if self.verbose:
            print("X Shape: ", X.shape)
        self.f_labels = self._generate_feature_labels(X)
        return self

    def transform(self, X):
        '''
        Transform the segmented time series data into feature data.
        If contextual data is included in X, it is returned with the feature data.

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Segmented time series data and (optionally) contextual data

        Returns
        -------
        X_new : array shape [n_series, ...]
            Feature representation of segmented time series data and contextual data

        '''
        self._check_if_fitted()
        Xt, Xc = get_ts_data_parts(X)
        check_array(Xt, dtype='numeric', ensure_2d=False, allow_nd=True)

        fts = np.column_stack([self.features[f](Xt) for f in self.features])
        if Xc is not None:
            fts = np.column_stack([fts, Xc])
        return fts

    def _reset(self):
        ''' Resets internal data-dependent state of the transformer. __init__ parameters not
        touched. '''
        self.f_labels = None

    def _check_if_fitted(self):
        if self.f_labels is None:
            raise NotFittedError("FeatureRep")

    def _check_features(self, features, Xti):
        '''
        tests output of each feature against a segmented time series X

        Parameters
        ----------
        features : dict
            feature function dictionary
        Xti : array-like, shape [n_samples, segment_width, n_variables]
            segmented time series (instance)

        Returns
        -------
            ftr_sizes : dict
                number of features output by each feature function
        '''
        N = Xti.shape[0]
        N_fts = len(features)
        fshapes = np.zeros((N_fts, 2), dtype=np.int)
        keys = [key for key in features]
        for i in np.arange(N_fts):
            fshapes[i] = np.row_stack(features[keys[i]](Xti)).shape

        # make sure each feature returns an array shape [N, ]
        if not np.all(fshapes[:, 0] == N):
            raise ValueError("feature function returned array with invalid length, ",
                             np.array(features.keys())[fshapes[:, 0] != N])

        return {keys[i]: fshapes[i, 1] for i in range(N_fts)}

    def _generate_feature_labels(self, X):
        '''
        Generates string feature labels
        '''
        Xt, Xc = get_ts_data_parts(X)

        ftr_sizes = self._check_features(self.features, Xt[0:3])
        f_labels = []

        # calculated features
        for key in ftr_sizes:
            for i in range(ftr_sizes[key]):
                f_labels += [key + '_' + str(i)]

        # contextual features
        if Xc is not None:
            Ns = len(np.atleast_1d(Xc[0]))
            s_labels = ["context_" + str(i) for i in range(Ns)]
            f_labels += s_labels

        return f_labels


class FeatureRepMix(_BaseComposition, TransformerMixin):
    '''
    A transformer for calculating a feature representation from segmented time series data.

    This transformer calculates features from the segmented time series', by applying the supplied
    list of FeatureRep transformers on the specified columns of data. Non-specified columns are
    dropped.

    The segmented time series data is expected to enter this transform in the form of
    num_samples x segment_size x num_features and to leave this transform in the form of
    num_samples x num_features. The term columns refers to the last dimension of both
    representations.

    Note: This code is partially taken (_validate and _transformers functions with docstring) from
          the scikit-learn ColumnTransformer made available under the 3-Clause BSD license.

    Parameters
    ----------
    transformers : list of (name, transformer, columns) to be applied on the segmented time series
        name : string
            unique string which is used to prefix the f_labels of the FeatureRep below
        transformer : FeatureRep transform
            to be applied on the columns specified below
        columns : integer, slice or boolean mask
            to specify the columns to be transformed

    Attributes
    ----------
    f_labels : list of string feature labels (in order) corresponding to the computed features

    Examples
    --------

    >>> from seglearn.transform import FeatureRepMix, FeatureRep, SegmentX
    >>> from seglearn.pipe import Pype
    >>> from seglearn.feature_functions import mean, var, std, skew
    >>> from seglearn.datasets import load_watch
    >>> from sklearn.ensemble import RandomForestClassifier
    >>> data = load_watch()
    >>> X = data['X']
    >>> y = data['y']
    >>> mask = [False, False, False, True, True, True]
    >>> clf = Pype([('seg', SegmentX()),
    >>>             ('union', FeatureRepMix([
    >>>                 ('ftr_a', FeatureRep(features={'mean': mean}), 0),
    >>>                 ('ftr_b', FeatureRep(features={'var': var}), [0,1,2]),
    >>>                 ('ftr_c', FeatureRep(features={'std': std}), slice(3,7)),
    >>>                 ('ftr_d', FeatureRep(features={'skew': skew}), mask),
    >>>             ])),
    >>>             ('rf',RandomForestClassifier())])
    >>> clf.fit(X, y)
    >>> print(clf.score(X, y))

    '''

    def __init__(self, transformers):
        self.transformers = transformers
        self.f_labels = None

    @property
    def _transformers(self):
        '''
        Internal list of transformers only containing the name and transformers, dropping the
        columns. This is for the implementation of get_params via BaseComposition._get_params which
        expects lists of tuples of len 2.
        '''
        return [(name, trans) for name, trans, _ in self.transformers]

    @_transformers.setter
    def _transformers(self, value):
        self.transformers = [
            (name, trans, col) for ((name, trans), (_, _, col))
            in zip(value, self.transformers)]

    def get_params(self, deep=True):
        '''
        Get parameters for this transformer.

        Parameters
        ----------
        deep : boolean, optional
            If True, will return the parameters for this transformer and contained transformers.

        Returns
        -------
        params : mapping of string to any parameter names mapped to their values.
        '''
        return self._get_params('_transformers', deep=deep)

    def set_params(self, **kwargs):
        '''
        Set the parameters of this transformer.

        Valid parameter keys can be listed with ``get_params()``.

        Returns
        -------
        self
        '''
        self._set_params('_transformers', **kwargs)
        return self

    @staticmethod
    def _select(Xt, cols):
        '''
        Select slices of the last dimension from time series data of the form
        num_samples x segment_size x num_features.
        '''
        return np.atleast_3d(Xt)[:, :, cols]

    @staticmethod
    def _retrieve_indices(cols):
        '''
        Retrieve a list of indices corresponding to the provided column specification.
        '''
        if isinstance(cols, int):
            return [cols]
        elif isinstance(cols, slice):
            start = cols.start if cols.start else 0
            stop = cols.stop
            step = cols.step if cols.step else 1
            return list(range(start, stop, step))
        elif isinstance(cols, list) and cols:
            if isinstance(cols[0], bool):
                return np.flatnonzero(np.asarray(cols))
            elif isinstance(cols[0], int):
                return cols
        else:
            raise TypeError('No valid column specifier. Only a scalar, list or slice of all'
                            'integers or a boolean mask are allowed.')

    def fit(self, X, y=None):
        '''
        Fit the transform

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Segmented time series data and (optionally) contextual data
        y : None
            There is no need of a target in a transformer, yet the pipeline API requires this
            parameter.

        Returns
        -------
        self : object
            Returns self.
        '''
        Xt, Xc = get_ts_data_parts(X)
        self.f_labels = []

        # calculated features (prefix with the FeatureRep name and correct the index)
        for name, trans, cols in self.transformers:
            indices = self._retrieve_indices(cols)
            trans.fit(self._select(Xt, cols))
            for label, index in zip(trans.f_labels, indices):
                self.f_labels.append(name + '_' + label.rsplit('_', 1)[0] + '_' + str(index))

        # contextual features
        if Xc is not None:
            Ns = len(np.atleast_1d(Xc[0]))
            self.f_labels += ['context_' + str(i) for i in range(Ns)]

        return self

    def _validate(self):
        '''
        Internal function to validate the transformer before applying all internal transformers.
        '''
        if self.f_labels is None:
            raise NotFittedError('FeatureRepMix')

        if not self.transformers:
            return

        names, transformers, _ = zip(*self.transformers)

        # validate names
        self._validate_names(names)

        # validate transformers
        for trans in transformers:
            if not isinstance(trans, FeatureRep):
                raise TypeError("All transformers must be an instance of FeatureRep."
                                " '%s' (type %s) doesn't." % (trans, type(trans)))

    def transform(self, X):
        '''
        Transform the segmented time series data into feature data.
        If contextual data is included in X, it is returned with the feature data.

        Parameters
        ----------
        X : array-like, shape [n_series, ...]
            Segmented time series data and (optionally) contextual data

        Returns
        -------
        X_new : array shape [n_series, ...]
            Feature representation of segmented time series data and contextual data

        '''
        self._validate()

        Xt, Xc = get_ts_data_parts(X)
        check_array(Xt, dtype='numeric', ensure_2d=False, allow_nd=True)

        # calculated features
        fts = np.column_stack([trans.transform(self._select(Xt, cols))
                               for _, trans, cols in self.transformers])
        # contextual features
        if Xc is not None:
            fts = np.column_stack([fts, Xc])

        return fts


class FunctionTransformer(BaseEstimator, TransformerMixin):
    '''
    Transformer for applying a custom function to time series data.

    Parameters
    ----------
    func : function, optional (default=None)
        the function to be applied to Xt, the time series part of X (contextual variables Xc are
        passed through unaltered) - X remains unchanged if no function is supplied
    func_kwargs : dictionary, optional (default={})
        keyword arguments to be passed to the function call

    Returns
    -------
    self : object
        returns self

    Examples
    --------

    >>> from seglearn.transform import FunctionTransformer
    >>> import numpy as np
    >>>
    >>> def choose_cols(Xt, cols):
    >>>     return [time_series[:, cols] for time_series in Xt]
    >>>
    >>> X = [np.array([[0, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10, 11]]),
    >>>     np.array([[30, 40, 50], [60, 70, 80], [90, 100, 110]])]
    >>> y = [np.array([True, False, False, True]),
    >>>     np.array([False, True, False])]
    >>> trans = FunctionTransformer(choose_cols, func_kwargs={"cols":[0,1]})
    >>> X = trans.fit_transform(X, y)

    '''

    def __init__(self, func=None, func_kwargs={}):
        self.func = func
        self.func_kwargs = func_kwargs

    def fit(self, X, y=None):
        '''
        Fit the transform

        Parameters
        ----------
        X : array-like, shape [n_samples, ...]
            time series data and (optionally) contextual data
        y : None
            there is no need of a target in a transformer, yet the pipeline API requires this

        Returns
        -------
        self : object
            returns self
        '''
        check_ts_data(X, y)
        return self

    def transform(self, X):
        '''
        Transforms the time series data based on the provided function. Note this transformation
        must not change the number of samples in the data.

        Parameters
        ----------
        X : array-like, shape [n_samples, ...]
            time series data and (optionally) contextual data

        Returns
        -------
        Xt : array-like, shape [n_samples, ...]
            transformed time series data

        '''
        if self.func is None:
            return X
        else:
            Xt, Xc = get_ts_data_parts(X)
            n_samples = len(Xt)
            Xt = self.func(Xt, **self.func_kwargs)
            if len(Xt) != n_samples:
                raise ValueError("FunctionTransformer changes sample number (not supported).")
            if Xc is not None:
                Xt = TS_Data(Xt, Xc)
            return Xt


class _InitializePickableSampler(object):
    '''
    Class for initializing a serialized/pickled and dynamically patched imbalanced-learn Sampler.
    '''
    def __call__(self, sampler_class):
        '''
        Recreate a dynamically patched Sampler by creating a _InitializePickableSampler object and
        turning it into a patched Sampler by using the patch_sampler function.
        '''
        obj = _InitializePickableSampler()
        obj.__class__ = patch_sampler(sampler_class)
        return obj


def patch_sampler(sampler_class):
    '''
    Return a dynamically patched imbalanced-learn Sampler class compatible with Pype.
    '''
    if not hasattr(sampler_class, 'fit_resample') or not hasattr(sampler_class, '_check_X_y'):
        raise TypeError('The sampler class to be patched must have a "fit_resample" and a'
                        ' "_check_X_y" method')

    class PickableSampler(sampler_class, XyTransformerMixin):
        '''
        Dynamically created (pickable) class derived from an imbalanced-learn Sampler and the
        XyTransformerMixin in order to enable the use of the imbalanced-learn Sampler transforms
        inside a seglearn Pype.

        Parameters
        ----------
        shuffle : boolean, optional (default=False)
        random_state : int, RandomState instance or None, optional (default=None)
            seed of the pseudo random number generator used for shuffling the resampled data
        **kwargs : keyword arguments to be passed to the imbalanced-learn Sampler base class

        Returns
        -------
        self : object
            returns self
        '''
        def __init__(self, shuffle=False, random_state=None, **kwargs):
            # set shuffle and random_state
            self.shuffle = shuffle
            self.random_state = random_state

            # call imbalanced-learn Sampler base class with the correct arguments
            orig_signature = signature(super(PickableSampler, self).__init__)
            orig_args = [p.name for p in orig_signature.parameters.values()
                         if p.name != 'self' and p.kind != p.VAR_KEYWORD]
            if "shuffle" in orig_args:
                kwargs["shuffle"] = shuffle
            if "random_state" in orig_args:
                kwargs["random_state"] = random_state
            super(PickableSampler, self).__init__(**kwargs)

        @staticmethod
        def _check_X_y(Xt, yt):
            '''
            Circumvent the check whether dim(Xt) == 2.
            '''
            Xt_2d = Xt.reshape(Xt.shape[0], -1)
            _, yt, binarize_yt = super(PickableSampler, PickableSampler)._check_X_y(Xt_2d, yt)
            Xt = check_array(Xt, dtype='numeric', ensure_2d=False, allow_nd=True)
            return Xt, yt, binarize_yt

        def transform(self, X, y=None, sample_weight=None):
            '''
            Return the given segmented time series data (identity transform) when calling transform
            without fit on this data (potentially making a prediction) to not alter test data.

            Parameters
            ----------
            X : array-like, shape [n_series, ...]
               time series data and (optionally) contextual data
            y : array-like, shape [n_series] (default=None)
                target vector
            sample_weight : array-like shape [n_series] (default=None)
                sample weights

            Returns
            -------
            X : array-like [n_series, ...]
            y : array-like [n_series]
            sample_weight : array-like shape [n_series]
            '''
            check_ts_data(X, y)
            return X, y, sample_weight

        def fit_transform(self, X, y, sample_weight=None, **fit_params):
            '''
            Resample the given segmented time series data based on the Sampler transformer provided
            as a bass class when calling fit (i.e. not making any prediction on the test data) on
            this transformer.
            Sample weights always returned as None.

            Parameters
            ----------
            X : array-like, shape [n_series, ...]
               time series data and (optionally) contextual data
            y : array-like, shape [n_series] (default=None)
                target vector
            sample_weight : array-like shape [n_series] (default=None)
                sample weights
            **fit_params : dict of string -> object
                parameters for the inner imbalanced-learn Sampler object

            Returns
            -------
            X : array-like [n_series, ...]
            y : array-like [n_series]
            sample_weight : None
            '''
            check_ts_data(X, y)
            Xt, Xc = get_ts_data_parts(X)
            Xt, yt = super(PickableSampler, self).fit_resample(Xt, y, **fit_params)
            if self.shuffle:
                Xt, yt = shuffle(Xt, yt, random_state=self.random_state)
            if Xc is not None:
                Xt = TS_Data(Xt, Xc)
            return Xt, yt, None

        def __reduce__(self):
            '''
            Definition on how to serialize/pickle an object of this dynamically created class.
            '''
            return (_InitializePickableSampler(), (sampler_class,), self.__dict__)

    new_class_name = "Patched" + sampler_class.__name__
    PickableSampler.__name__ = new_class_name
    PickableSampler.__qualname__ = new_class_name
    return PickableSampler
