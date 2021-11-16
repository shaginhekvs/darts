"""
Kalman Filter
-------------
"""

from abc import ABC
from copy import deepcopy
from typing import Optional, Sequence, Union

import numpy as np
import pandas as pd
from nfoursid.kalman import Kalman
from nfoursid.nfoursid import NFourSID

from darts.models.filtering.filtering_model import FilteringModel
from darts.timeseries import TimeSeries
from darts.utils.utils import raise_if_not


class KalmanFilter(FilteringModel, ABC):
    def __init__(
            self, 
            # TODO: add input dimension?
            dim_x: int = 1,
            dim_y: int = 1,  # TODO: infer at fitting time?
            # x_init: Optional[np.array] = None,
            kf: Optional[Kalman] = None
            ):
        """
        This model implements a Kalman filter over a time series (without control signal).

        The key method is `KalmanFilter.filter()`.
        It considers the provided time series as containing (possibly noisy) observations z obtained from a
        (possibly noisy) linear dynamical system with hidden state x. The function `filter(series)` returns a new
        `TimeSeries` describing the distribution of the state x, as inferred by the Kalman filter from
        sequentially observing z from `series`.
        Depending on the use case, this can be used to de-noise a series or infer the underlying hidden state of the
        data generating process (assuming notably that the dynamical system generating the data is known, as captured
        by the `F` matrix.).

        This implementation wraps around filterpy.kalman.KalmanFilter, so more information the parameters can be found
        here: https://filterpy.readthedocs.io/en/latest/kalman/KalmanFilter.html

        The dimensionality of the measurements z is automatically inferred upon calling `filter()`.
        This implementation doesn't include control signal.

        Parameters
        ----------
        dim_x : int
            Size of the Kalman filter state vector. It determines the dimensionality of the `TimeSeries`
            returned by the `filter()` function.
        x_init : ndarray (dim_x, 1), default: [0, 0, ..., 0]
            Initial state; will be updated at each time step.
        P : ndarray (dim_x, dim_x), default: identity matrix
            initial covariance matrix; will be update at each time step
        Q : ndarray (dim_x, dim_x), default: identity matrix
            Process noise covariance matrix
        R : ndarray (dim_z, dim_z), default: identity matrix
            Measurement noise covariance matrix. `dim_z` must match the dimensionality (width) of the `TimeSeries`
            used with `filter()`.
        H : ndarray (dim_z, dim_x), default: all-ones matrix
            measurement function; describes how the measurement z is obtained from the state vector x
        F : ndarray (dim_x, dim_x), default: identity matrix
            State transition matrix; describes how the state evolves from one time step to the next
            in the underlying dynamical system.
        kf : filterpy.kalman.KalmanFilter
            Optionally, an instance of `filterpy.kalman.KalmanFilter`.
            If this is provided, the other parameters are ignored. This instance will be copied for every
            call to `filter()`, so the state is not carried over from one time series to another across several
            calls to `filter()`.
            The various dimensionality in the filter must match those in the `TimeSeries` used when calling `filter()`.
        """
        super().__init__()
        if kf is None:
            self.dim_x = dim_x
            self.dim_y = dim_y
            self.kf_provided = False
        else:
            self.kf = kf
            self.dim_x = kf.state_space.x_dim
            self.dim_y = kf.state_space.y_dim
            self.kf_provided = True

    def __str__(self):
        return 'KalmanFilter(dim_x={})'.format(self.dim_x)

    def fit(self,
            series: Union[TimeSeries, Sequence[TimeSeries]],
            covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None):
        
        #TODO: If multiple timeseries, loop over

        outputs = series.pd_dataframe()
        outputs.columns = [f'y_{i}' for i in outputs.columns]
        if covariates is not None:
            inputs = covariates.pd_dataframe()
            inputs.columns = [f'u_{i}' for i in inputs.columns]
            input_columns = inputs.columns
            measurements = pd.concat([outputs, inputs], axis=1)
        else:
            measurements = outputs
            input_columns = None

        nfoursid = NFourSID(measurements,
                            output_columns=outputs.columns,
                            input_columns=input_columns,
                            num_block_rows=10) #TODO: make num_block_rows parameter
        nfoursid.subspace_identification()
        state_space_identified, covariance_matrix = nfoursid.system_identification(
            rank=self.dim_x
        )

        self.kf = Kalman(state_space_identified, covariance_matrix)


    def filter(self,
               series: Union[TimeSeries, Sequence[TimeSeries]],
               covariates: Optional[Union[TimeSeries, Sequence[TimeSeries]]] = None,
               num_samples: int = 1):
        """
        Sequentially applies the Kalman filter on the provided series of observations.

        Parameters
        ----------
        series : TimeSeries
            The series of observations used to infer the state values according to the specified Kalman process.
            This must be a deterministic series (containing one sample).

        Returns
        -------
        TimeSeries
            A stochastic `TimeSeries` of state values, of dimension `dim_x`.
        """

        raise_if_not(series.is_deterministic, 'The input series for the Kalman filter must be '
                                              'deterministic (observations).')

        if not self.kf_provided: #TODO: simplify to is None?
            #TODO: raise error model not fitted
            pass
        else:
            raise_if_not(series.width == self.dim_y, 'The provided TimeSeries dimensionality does not match '
                                                     'the output dimensionality of the Kalman filter.')
        kf = deepcopy(self.kf)

        super().filter(series)
        y_values = series.values(copy=False)
        if covariates is None:
            u_values = np.zeros((len(y_values), 0))
        else:
            u_values = covariates.values(copy=False) #TODO: check lengths are equal
        
        # For each time step, we'll sample "n_samples" from a multivariate Gaussian
        # whose mean vector and covariance matrix come from the Kalman filter.
        if num_samples == 1:
            sampled_states = np.zeros(((len(y_values)), self.dim_y, ))
        else:
            sampled_states = np.zeros(((len(y_values)), self.dim_y, num_samples))

        # process_means = np.zeros((len(values), self.dim_x))  # mean values
        # process_covariances = ...                            # covariance matrices; TODO
        for i in range(len(y_values)):
            y = y_values[i, :].reshape(-1, 1)
            u = u_values[i, :].reshape(-1, 1)
            kf.step(y, u)
            mean_vec = kf.y_filtereds[-1].reshape(self.dim_y,)

            if num_samples == 1:
                # It's actually not sampled in this case
                sampled_states[i, :] = mean_vec
            else:
                # TODO: check formula for covariance matrix
                cov_matrix = kf.state_space.c @ kf.p_filtereds[-1] @ kf.state_space.c.T + kf.r
                sampled_states[i, :, :] = np.random.multivariate_normal(mean_vec, cov_matrix, size=num_samples).T

        # TODO: later on for a forecasting model we'll have to do something like
        """
        for _ in range(horizon):
            kf.predict()
            # forecasts on the observations, obtained from the state
            preds.append(kf.H.dot(kf.x))
            preds_cov.append(kf.H.dot(kf.P).dot(kf.H.T))
        """
        # TODO: test cases
        # - with/without input (check on conistency + dim between fit/filter)
        # - single/multiple timeseries at fit/predict time

        return TimeSeries.from_times_and_values(series.time_index, sampled_states)
