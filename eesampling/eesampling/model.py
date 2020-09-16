import copy
import pandas as pd
import itertools
import logging
from plotnine import *
import plotnine
import numpy as np
from .bins import Binning, BinnedData, ModelSamplingException, sample_bins
from .diagnostics import Diagnostics

pd.options.mode.chained_assignment = None  # suppress warnings

logger = logging.getLogger(__name__)


class StratifiedSampling(object):
    def __init__(self, train_label="train", test_label="test", output_name="output"):
        self.columns = {}
        self.train_label = train_label
        self.test_label = test_label
        self.output_name = output_name
        self.trained = False
        self.sampled = False
        self.data_train = None
        self.data_test = None
        self.data_sample = None

    def _chop_outliers(self, df):
        for name, c in self.columns.items():
            if c["min_value_allowed"] is not None:
                df = df[df[c["name"]] >= c["min_value_allowed"]]
            if c["max_value_allowed"] is not None:
                df = df[df[c["name"]] <= c["max_value_allowed"]]
        return df

    def _perturb(self, df_orig, col_names=None, random_seed=1):
        # qcut doesn't work if the same value recurs too many times, i.e. zero.  We can add a small amount of random noise to fix this
        np.random.seed(random_seed)
        df_pert = df_orig.copy()
        col_names = col_names if col_names else list(self.columns.keys())
        for col_name in col_names:
            range = df_pert[col_name].max() - df_pert[col_name].min()
            perturbation = (np.random.random(len(df_pert)) - 0.5) * range * 1e-6
            df_pert.loc[:, col_name] = df_pert[col_name] + perturbation
        return df_pert

    def add_column(
        self,
        name: str,
        n_bins: int = None,
        min_value_allowed: int = None,
        max_value_allowed: int = None,
        fixed_width: int = True,
        auto_bin_require_equivalence: bool = True,
    ):
        """
        Attributes
        ----------
        name: str
            The name of the column to be added to the model.
        n_bins: int
            Fixed number of bins to stratify over for this column.
            If set to None, automatic binning occurs. 
        min_value_allowed: int
            Minimum treatment value used to construct bins (used to remove outliers).
        max_value_allowed: int
            Maximum treatment value used to construct bins (used to remove outliers).
        auto_bin_require_equivalence: bool
            Whether the column requires equivalence when auto-binning
        """
        auto_bin = n_bins is None
        n_bins = 1 if n_bins is None else n_bins

        self.columns[name] = {
            "name": name,
            "auto_bin": auto_bin,
            "n_bins": n_bins,
            "min_value_allowed": min_value_allowed,
            "max_value_allowed": max_value_allowed,
            "fixed_width": fixed_width,
            "auto_bin_require_equivalence": auto_bin_require_equivalence,
        }

        self.binning = None
        self.trained = False
        self.predicted = False
        self.col_names = list(self.columns.keys())
        return self

    def _check_columns_present(self, df):
        if not getattr(self, "col_names"):
            raise ValueError(
                "No columns found in model. Use add_columns(...) to add a column."
            )
        missing_cols = list(set(self.col_names) - set(df.columns))
        if len(missing_cols) > 0:
            raise ValueError(
                f"data is missing required columns: {','.join(missing_cols)}"
            )

    def fit_and_sample(
        self,
        df_train,
        df_test,
        n_samples_approx=None,
        min_n_train_per_bin=0,
        random_seed=1,
        min_n_sampled_to_n_train_ratio=4,
        relax_n_samples_approx_constraint=False,
    ):
        """
        Attributes
        ----------
        df_train: pandas.DataFrame
            dataframe to use for constructing the stratified sampling bins.
        df_test: pandas.DataFrame
            dataframe to sample from according to the constructed stratified sampling bins.
        n_samples_aprox: int
            approximate number of total samples from df_test. It is approximate because
            there may be some slight discrepencies around the total count to ensure
            that each bin has the correct percentage of the total.
        min_n_train_per_bin: int
            Minimum number of training samples that must exist in a given bin for 
            it to be considered a non-outlier bin (only applicable if there are 
            cols with fixed_width=True)
        min_n_sampled_to_n_train_ratio: int
        relax_n_samples_approx_constraint: bool
            If True, treats n_samples_approx as an upper bound, but gets as many comparison group
            meters as available up to n_samples_approx. If False, it raises an exception
            if there are not enough comparison pool meters to reach n_samples_approx.
            
        """
        if len(self.columns) == 0:
            raise ValueError("You must add at least one column before fitting.")
        logger.debug(self.columns)
        for name, col in self.columns.items():
            if col["auto_bin"]:
                completed = False
                while not completed:
                    logging.info(f"Computing bins: {self.get_all_n_bins_as_str()} ")
                    self.fit(
                        df_train,
                        min_n_train_per_bin=min_n_train_per_bin,
                        random_seed=random_seed,
                    )
                    self.sample(
                        df_test,
                        n_samples_approx=n_samples_approx,
                        random_seed=random_seed,
                        relax_n_samples_approx_constraint=relax_n_samples_approx_constraint,
                    )

                    def _violates_ratio():
                        n_sampled_to_n_train_ratio = (
                            self.diagnostics().n_sampled_to_n_train_ratio()
                        )
                        if n_sampled_to_n_train_ratio < min_n_sampled_to_n_train_ratio:
                            logger.info(
                                f"Insufficient test data in one of the bins for {col['name']}:"
                                f"found {n_sampled_to_n_train_ratio}:1 but need "
                                f"{min_n_sampled_to_n_train_ratio}:1. Using last successful n_bins."
                            )
                            return True
                        return False

                    if col["auto_bin_require_equivalence"]:
                        if self.data_sample.df.empty:
                            raise ValueError(
                                "Too many bin divisions before finding equivalence"
                                f" for {col['name']} (usually occurs when several"
                                " stratification params are used)."
                            )
                        completed = self.diagnostics().equivalence_passed([col["name"]])
                        if min_n_sampled_to_n_train_ratio and _violates_ratio():
                            completed = True
                            self.set_n_bins(name, self.get_n_bins(name) - 1)
                        if not completed:
                            self.set_n_bins(name, self.get_n_bins(name) + 1)
                    else:
                        if min_n_sampled_to_n_train_ratio and _violates_ratio():
                            self.set_n_bins(name, self.get_n_bins(name) - 1)
                            completed = True
                        else:
                            self.set_n_bins(name, self.get_n_bins(name) + 1)

        self.fit(
            df_train, min_n_train_per_bin=min_n_train_per_bin, random_seed=random_seed
        )
        n_treatment = len(df_train)
        # if n_samples_approx is None, use the maximum available.
        df_sample = self.sample(
            df_test, n_samples_approx=n_samples_approx, random_seed=random_seed,
                        relax_n_samples_approx_constraint=relax_n_samples_approx_constraint,
        )
        self.n_samples_approx = n_samples_approx
        return df_sample

    def print_n_bins(self):
        logger.info(self.get_all_n_bins_as_str())

    def get_all_n_bins_as_str(self):
        return ",".join(
            [f"{col}:{self.get_n_bins(col)} bins" for col in self.columns.keys()]
        )

    def get_n_bins(self, col_name):
        col = self.columns[col_name]
        return col["n_bins"]

    def set_n_bins(self, col_name, n_bins):
        col = self.columns[col_name]
        col["n_bins"] = n_bins
        self.columns[col_name] = col

    def fit(self, df_train, min_n_train_per_bin=0, random_seed=1):
        self._check_columns_present(df_train)
        df_train = self._perturb(self._chop_outliers(df_train), random_seed=random_seed)
        self.df_train = df_train.copy()
        self.binning = Binning()

        self.df_train["_outlier_value"] = False
        for name, col in self.columns.items():

            if col["min_value_allowed"] is not None:
                self.df_train.loc[
                    self.df_train[col["name"]] < col["min_value_allowed"],
                    "_outlier_value",
                ] = True
            if col["max_value_allowed"] is not None:
                self.df_train.loc[
                    self.df_train[col["name"]] > col["max_value_allowed"],
                    "_outlier_value",
                ] = True

        for name, col in self.columns.items():
            values = (
                self.df_train.loc[~self.df_train._outlier_value, col["name"]]
                .dropna()
                .astype(float)
            )
            self.binning.bin(
                values, col["name"], col["n_bins"], fixed_width=col["fixed_width"]
            )

        self.data_train = BinnedData(
            self.df_train, self.binning, min_n_train_per_bin=min_n_train_per_bin
        )
        self.trained = True

    # what kinds of diagnostics?
    # - explore raw training data
    # - explore raw test data
    # - compare training data vs test data, pre-fit
    # - compare training data vs test data, post-fit
    # - compare training data vs test data, post-sampled

    def diagnostics(self):
        return Diagnostics(model=self)

    def sample(self, df_test, n_samples_approx=None, random_seed=1, relax_n_samples_approx_constraint=False):
        if not self.trained and data_train is not None:
            raise ValueError("No model found; please run fit()")
        self._check_columns_present(df_test)
        df_test = self._perturb(self._chop_outliers(df_test), random_seed=random_seed)
        self.data_test = BinnedData(df_test, self.binning)
        df_sample = sample_bins(
            self.data_train,
            self.data_test,
            n_samples_approx=n_samples_approx,
            relax_n_samples_approx_constraint=relax_n_samples_approx_constraint,
            random_seed=random_seed,
        )
        self.data_sample = BinnedData(df_sample, self.binning)
        self.sampled = True
        return self.data_sample
