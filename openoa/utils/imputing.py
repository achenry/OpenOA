"""
This module provides methods for filling in null data with interpolated (imputed) values.
"""

from copy import deepcopy
from concurrent.futures import ProcessPoolExecutor
import multiprocessing
import numpy as np
import pandas as pd
import polars as pl
import polars.selectors as cs
from numpy.polynomial import Polynomial
from mpi4py import MPI
from mpi4py.futures import MPICommExecutor
import numexpr as ne

ne.set_num_threads(ne.detect_number_of_cores())

def impute_data(
    target_col: str,
    reference_col: str,
    target_data: pd.DataFrame = None,
    reference_data: pd.DataFrame = None,
    align_col: str = None,
    method: str = "linear",
    degree: int = 1,
    data: pd.DataFrame = None,
) -> pd.Series:  # ADD LINEAR FUNCTIONALITY AS DEFAULT, expection otherwise
    """Replaces NaN data in a target Pandas series with imputed data from a reference Panda series based on a linear
    regression relationship.

    Steps include:

    1. Merge the target and reference data frames on <align_col>, which is shared between the two
    2. Determine the linear regression relationship between the target and reference data series
    3. Apply that relationship to NaN data in the target series for which there is finite data in the reference series
    4. Return the imputed results as well as the index matching the target data frame

    Args:
        target_col(:obj:`str`): the name of the column in either :py:attr:`data` or
            :py:attr:`target_data` to be imputed.
        reference_col(:obj:`str`): the name of the column in either :py:attr:`data` or
            :py:attr:`reference_data` to be used for imputation.
        data(:obj:`pandas.DataFrame`): input data frame such as :py:attr:`PlantData.scada` that uses a
            MultiIndex with a timestamp and asset_id column for indices, in that order, by default None.
        target_data(:obj:`pandas.DataFrame`): the ``DataFrame`` with  NaN data to be imputed.
        reference_data(:obj:`pandas.DataFrame`): the ``DataFrame`` to be used in imputation
        align_col(:obj:`str`): the name of the column that to join :py:attr:`target_data` and :py:attr:`reference_data`.

    Returns:
        :obj:`pandas.Series`: Copy of target_data_col series with NaN occurrences imputed where possible.
    """
    final_col_name = deepcopy(target_col)
    if data is None:
        if any(not isinstance(x, pd.DataFrame) for x in (target_data, reference_data)):
            raise TypeError(
                "If `data` is not provided, then `ref_data` and `target_data` must be provided as pandas DataFrames."
            )
        if target_col not in target_data:
            raise ValueError("The input `target_col` is not a column of `target_data`.")
        if reference_col not in reference_data:
            raise ValueError("The input `reference_col` is not a column of `ref_data`.")
        if align_col is not None:
            if align_col not in target_data and align_col not in target_data.index.names:
                raise ValueError(
                    "The input `align_col` is not a column or index of one of `target_data`."
                )
            if align_col not in reference_data and align_col not in reference_data.index.names:
                raise ValueError(
                    "The input `align_col` is not a column or index of one of `reference_data`."
                )

        # Unify the data, if the target and reference data are provided separately
        data = pd.merge(
            target_data,
            reference_data,
            on=align_col,
            how="left",
            left_index=align_col is None,
            right_index=align_col is None,
        )
        data.index = target_data.index

        # If the input and reference series are names the same, adjust their names to match the
        # result from merging
        if target_col == reference_col:
            final_col_name = deepcopy(target_col)
            target_col = target_col + "_x"  # Match the merged column name
            reference_col = reference_col + "_y"  # Match the merged column name

    if target_col not in data:
        raise ValueError("The input `target_col` is not a column of `data`.")
    if reference_col not in data:
        raise ValueError("The input `reference_col` is not a column of `data`.")

    data = data.loc[:, [reference_col, target_col]]
    data_reg = data.dropna()
    if data_reg.empty:
        raise ValueError("Not enough data to create a curve fit.")

    # Ensure old method call will work here
    if method == "linear":
        method = "polynomial"
        degree = 1
    if method == "polynomial":
        curve_fit = Polynomial.fit(data_reg[reference_col], data_reg[target_col], degree)
    else:
        raise NotImplementedError(
            "Only 'linear' (1-degree polynomial) and 'polynomial' fits are implemented at this time."
        )

    imputed = data.loc[
        (data[target_col].isnull() & np.isfinite(data[reference_col])), [reference_col]
    ]
    data.loc[imputed.index, target_col] = curve_fit(imputed[reference_col])
    return data.loc[:, target_col].rename(final_col_name)

def impute_all_assets_by_correlation(
    data_pd: pd.DataFrame | None,
    data_pl: pl.LazyFrame | None,
    impute_col: str,
    reference_col: str,
    asset_id_col: str = "asset_id",
    r2_threshold: float = 0.7,
    method: str = "linear",
    degree: int = 1,
    multiprocessor: str | None = None,
):
    """Imputes NaN data in a Pandas data frame to the best extent possible by considering available data
    across different assets in the data frame. Highest correlated assets are prioritized in the imputation process.

    Steps include:

    1. Establish correlation matrix of specified data between different assets
    2. For each asset in the data frame, sort neighboring assets by correlation strength
    3. Then impute asset data based on available data in the highest correlated neighbor
    4. If NaN data still remains in asset, move on to next highest correlated neighbor, etc.
    5. Continue until either:
        a. There are no NaN data remaining in asset data
        b. There are no more neighbors to consider
        c. The neighboring asset does not meet the specified correlation threshold, :py:attr:`r2_threshold`

    Args:
        data(:obj:`pandas.DataFrame`): input data frame such as :py:attr:`PlantData.scada` that uses a
            MultiIndex with a timestamp and asset_id column for indices, in that order.
        impute_col(:obj:`str`): the name of the column in `data` to be imputed.
        reference_col(:obj:`str`): the name of the column in `data` to be used in imputation.
        asset_id_col(:obj:`str): The name of the asset_id column, should be one of the turinbe or tower
            index column names. Defaults to the turbine column name "asset_id".
        r2_threshold(:obj:`float`): the correlation threshold for a neighboring assets to be considered valid
            for use in imputation, by default 0.7.
        method(:obj:`str`): The imputation method, should be one of "linear" or "polynomial", by default "linear".
        degree(:obj:`int`): The polynomial degree, i.e. linear is a 1 degree polynomial, by default 1

    Returns:
        :obj:`pandas.Series`: The imputation results

    """

    if (data_pd is None and data_pl is None) or (data_pd is not None and data_pl is not None):
        raise Exception("Must provide either a pandas DataFrame or a polars LazyFrame, but not both")

    if data_pd is not None:
        # impute_df = data_pd.loc[:, :].copy()

        # Create correlation matrix between different assets
        corr_df = asset_correlation_matrix_pd(data_pd, impute_col)

        # Sort the correlated values according to the highest value, with nans at the end.
        ix_sort = (-corr_df.fillna(-2)).values.argsort(axis=1)
        sort_df = pd.DataFrame(corr_df.columns.to_numpy()[ix_sort], index=corr_df.index)
        data = data_pd
        impute_func = impute_target_id_pd
    elif data_pl is not None:
        # impute_df = None

        # Create correlation matrix between different assets
        corr_df = asset_correlation_matrix_pl(data_pl, impute_col)

        # Sort the correlated values according to the highest value, with nans at the end.
        ix_sort = (-corr_df.fillna(-2)).values.argsort(axis=1)
        sort_df = pd.DataFrame(corr_df.columns.to_numpy()[ix_sort], index=corr_df.columns)
        data = data_pl
        impute_func = impute_target_id_pl

    # Loop over the assets and impute missing data
    if multiprocessor is not None:
        if multiprocessor == "mpi":
            executor = MPICommExecutor(MPI.COMM_WORLD, root=0)
        else:  # "cf" case
            max_workers = multiprocessing.cpu_count()
            executor = ProcessPoolExecutor(max_workers=max_workers)
        with executor as ex:
            if ex is not None:
                futures = {"target_id": ex.submit(impute_func, 
                                            data=data,
                                            corr_df=corr_df,
                                            sort_df=sort_df,
                                            r2_threshold=r2_threshold,
                                            asset_id_col=asset_id_col,
                                            impute_df=data, impute_col=impute_col, reference_col=reference_col,
                                            target_id=target_id, method=method, degree=degree) 
                                            for target_id in corr_df.columns}
                
                for k, fut in futures.items():
                    res = fut.result()
                    if res is None:
                        continue
                    _, sub_df = res
                    data = data.update(sub_df.rename({impute_col: f"{impute_col}_{k}"}), on="time")
    else:
        for target_id in corr_df.columns:
            
            res = impute_func(data=data,
                                corr_df=corr_df,
                                sort_df=sort_df,
                                r2_threshold=r2_threshold,
                                asset_id_col=asset_id_col,
                                impute_df=data, impute_col=impute_col, reference_col=reference_col,
                                target_id=target_id, method=method, degree=degree)
            if res is None:
                continue
            
            _, sub_df = res
            data = data.update(sub_df.rename({impute_col: f"{impute_col}_{target_id}"}), on="time")

    # Return the results with the impute_col renamed with a leading "imputed_" for clarity
    # return impute_df.rename(columns={c: f"imputed_{c}" for c in impute_df.columns})
    return data

def impute_target_id_pl(data, corr_df, sort_df, r2_threshold, asset_id_col, impute_df, impute_col, reference_col, target_id, method, degree):
    print(f"Imputing feature {impute_col} for asset {target_id}")
    # If there are no NaN values, then skip the asset altogether, otherwise
    # keep track of the number we need to continue checking for
    ix_target = cs.ends_with(target_id).alias(impute_col)
    target_df = data.select("time", cs.ends_with(target_id).alias(impute_col)).collect().lazy()
    sub_df = target_df.clone()

    ix_nan = sub_df.select(pl.col(impute_col).is_null()).collect()
    any_nans = ix_nan.select(pl.col(impute_col).any()).item()
    if not any_nans:
        return

    # Get the correlation-based neareast neighbor and data
    id_sort_neighbor = 0
    id_neighbor = sort_df.loc[target_id, id_sort_neighbor]
    r2_neighbor = corr_df.loc[target_id, id_neighbor]

    # If the R2 value is too low, then move on to the next asset
    if r2_neighbor <= r2_threshold:
        return

    num_neighbors = corr_df.shape[0] - 1
    while (any_nans) & (num_neighbors > 0) & (r2_neighbor > r2_threshold):
        # Get the imputed data based on the correlation-based next nearest neighbor
        try:
            imputed_data = impute_data(
                # target_data=data.xs(target_id, level=1).loc[:, [impute_col]],
                target_data=target_df.select(impute_col).collect().to_pandas(),
                target_col=impute_col,
                # reference_data=data.xs(id_neighbor, level=1).loc[:, [reference_col]],
                reference_data=target_df.select(reference_col).collect().to_pandas(),
                reference_col=reference_col,
                method=method,
                degree=degree,
            )
        except ValueError as e:
            print(f"ValueError was raised while trying to impute {target_id}: {e}")
            break

        # Fill any NaN values with available imputed values
        sub_df = sub_df.with_columns(pl.when(ix_nan).then(imputed_data.values).otherwise(pl.col(impute_col)).alias(impute_col))

        ix_nan = sub_df.select(pl.col(impute_col).is_null()).collect()
        any_nans = ix_nan.select(pl.col(impute_col).any()).item()

        num_neighbors -= 1
        id_sort_neighbor += 1
        id_neighbor = sort_df.loc[target_id, id_sort_neighbor]
        r2_neighbor = corr_df.loc[target_id, id_neighbor]

    return ix_target, sub_df

def impute_target_id_pd(data, corr_df, sort_df, r2_threshold, asset_id_col, impute_df, impute_col, reference_col, target_id, method, degree):
    print(f"Imputing feature {impute_col} for asset {target_id}")
    # If there are no NaN values, then skip the asset altogether, otherwise
    # keep track of the number we need to continue checking for
    ix_target = impute_df.index.get_level_values(1) == target_id
    sub_df = impute_df.loc[ix_target, [impute_col]]
    if (ix_nan := data.loc[ix_target, impute_col].isnull()).sum() == 0:
        return

    # Get the correlation-based neareast neighbor and data
    id_sort_neighbor = 0
    id_neighbor = sort_df.loc[target_id, id_sort_neighbor]
    r2_neighbor = corr_df.loc[target_id, id_neighbor]

    # If the R2 value is too low, then move on to the next asset
    if r2_neighbor <= r2_threshold:
        return

    num_neighbors = corr_df.shape[0] - 1
    while (ix_nan.sum() > 0) & (num_neighbors > 0) & (r2_neighbor > r2_threshold):
        # Get the imputed data based on the correlation-based next nearest neighbor
        try:
            imputed_data = impute_data(
                # target_data=data.xs(target_id, level=1).loc[:, [impute_col]],
                target_data=data.loc[
                    data.index.get_level_values(1) == target_id, [impute_col]
                ].droplevel(asset_id_col),
                target_col=impute_col,
                # reference_data=data.xs(id_neighbor, level=1).loc[:, [reference_col]],
                reference_data=data.loc[
                    data.index.get_level_values(1) == id_neighbor, [reference_col]
                ].droplevel(asset_id_col),
                reference_col=reference_col,
                method=method,
                degree=degree,
            )
        except ValueError as e:
            print(f"ValueError was raised while trying to impute {target_id}: {e}")
            break

        # Fill any NaN values with available imputed values
        sub_df = sub_df.where(
            ~ix_nan, imputed_data.to_frame()
        )

        ix_nan = sub_df[impute_col].isnull()
        num_neighbors -= 1
        id_sort_neighbor += 1
        id_neighbor = sort_df.loc[target_id, id_sort_neighbor]
        r2_neighbor = corr_df.loc[target_id, id_neighbor]

    return ix_target, sub_df 

def asset_correlation_matrix_pl(data: pl.LazyFrame, value_col: str) -> pd.DataFrame:
    """Create a correlation matrix on a MultiIndex `DataFrame` with time (or a different
    alignment value) and asset_id values as its indices, respectively.

    Args:
        data(:obj:`pandas.DataFrame`): input data frame such as :py:attr:`PlantData.scada` that uses a
            MultiIndex with a timestamp and asset_id column for indices, in that order.
        value_col(:obj:`str`): the column containing the data values to be used when
            assessing correlation

    Returns:
        :obj:`pandas.DataFrame`: Correlation matrix with <id_col> as index and column names
    """

    # corr_df = data.collect().pivot(on="turbine_id", index="time", values=value_col, sort_columns=True)\
    #                         .drop("time").to_pandas().corr()
    corr_df = data.select(cs.starts_with(value_col)).rename(lambda col: col.split("_")[-1]).collect().to_pandas().corr()
    np.fill_diagonal(corr_df.values, np.nan)
    return corr_df

def asset_correlation_matrix_pd(data: pd.DataFrame, value_col: str) -> pd.DataFrame:
    """Create a correlation matrix on a MultiIndex `DataFrame` with time (or a different
    alignment value) and asset_id values as its indices, respectively.

    Args:
        data(:obj:`pandas.DataFrame`): input data frame such as :py:attr:`PlantData.scada` that uses a
            MultiIndex with a timestamp and asset_id column for indices, in that order.
        value_col(:obj:`str`): the column containing the data values to be used when
            assessing correlation

    Returns:
        :obj:`pandas.DataFrame`: Correlation matrix with <id_col> as index and column names
    """
    corr_df = data.loc[:, [value_col]].unstack().corr(min_periods=2)
    corr_df = corr_df.droplevel(0).droplevel(0, axis=1)  # drop the added axes
    corr_df.index = corr_df.index.set_names(None)
    corr_df.columns = corr_df.index.set_names(None)
    np.fill_diagonal(corr_df.values, np.nan)
    return corr_df

def impute_all_assets_by_correlation_sequential(
    data: pd.DataFrame,
    impute_col: str,
    reference_col: str,
    asset_id_col: str = "asset_id",
    r2_threshold: float = 0.7,
    method: str = "linear",
    degree: int = 1,
):
    """Imputes NaN data in a Pandas data frame to the best extent possible by considering available data
    across different assets in the data frame. Highest correlated assets are prioritized in the imputation process.

    Steps include:

    1. Establish correlation matrix of specified data between different assets
    2. For each asset in the data frame, sort neighboring assets by correlation strength
    3. Then impute asset data based on available data in the highest correlated neighbor
    4. If NaN data still remains in asset, move on to next highest correlated neighbor, etc.
    5. Continue until either:
        a. There are no NaN data remaining in asset data
        b. There are no more neighbors to consider
        c. The neighboring asset does not meet the specified correlation threshold, :py:attr:`r2_threshold`

    Args:
        data(:obj:`pandas.DataFrame`): input data frame such as :py:attr:`PlantData.scada` that uses a
            MultiIndex with a timestamp and asset_id column for indices, in that order.
        impute_col(:obj:`str`): the name of the column in `data` to be imputed.
        reference_col(:obj:`str`): the name of the column in `data` to be used in imputation.
        asset_id_col(:obj:`str): The name of the asset_id column, should be one of the turinbe or tower
            index column names. Defaults to the turbine column name "asset_id".
        r2_threshold(:obj:`float`): the correlation threshold for a neighboring assets to be considered valid
            for use in imputation, by default 0.7.
        method(:obj:`str`): The imputation method, should be one of "linear" or "polynomial", by default "linear".
        degree(:obj:`int`): The polynomial degree, i.e. linear is a 1 degree polynomial, by default 1

    Returns:
        :obj:`pandas.Series`: The imputation results

    """
    impute_df = data.loc[:, :].copy()

    # Create correlation matrix between different assets
    corr_df = asset_correlation_matrix_pd(data, impute_col)

    # Sort the correlated values according to the highest value, with nans at the end.
    ix_sort = (-corr_df.fillna(-2)).values.argsort(axis=1)
    sort_df = pd.DataFrame(corr_df.columns.to_numpy()[ix_sort], index=corr_df.index)
    # Loop over the assets and impute missing data
    for target_id in corr_df.columns:
        # If there are no NaN values, then skip the asset altogether, otherwise
        # keep track of the number we need to continue checking for
        ix_target = impute_df.index.get_level_values(1) == target_id
        if (ix_nan := data.loc[ix_target, impute_col].isnull()).sum() == 0:
            continue

        # Get the correlation-based neareast neighbor and data
        id_sort_neighbor = 0
        id_neighbor = sort_df.loc[target_id, id_sort_neighbor]
        r2_neighbor = corr_df.loc[target_id, id_neighbor]

        # If the R2 value is too low, then move on to the next asset
        if r2_neighbor <= r2_threshold:
            continue

        num_neighbors = corr_df.shape[0] - 1
        while (ix_nan.sum() > 0) & (num_neighbors > 0) & (r2_neighbor > r2_threshold):
            # Get the imputed data based on the correlation-based next nearest neighbor
            imputed_data = impute_data(
                # target_data=data.xs(target_id, level=1).loc[:, [impute_col]],
                target_data=data.loc[
                    data.index.get_level_values(1) == target_id, [impute_col]
                ].droplevel(asset_id_col),
                target_col=impute_col,
                # reference_data=data.xs(id_neighbor, level=1).loc[:, [reference_col]],
                reference_data=data.loc[
                    data.index.get_level_values(1) == id_neighbor, [reference_col]
                ].droplevel(asset_id_col),
                reference_col=impute_col,
                method=method,
                degree=degree,
            )

            # Fill any NaN values with available imputed values
            impute_df.loc[ix_target, [impute_col]] = impute_df.loc[ix_target, [impute_col]].where(
                ~ix_nan, imputed_data.to_frame()
            )

            ix_nan = impute_df.loc[ix_target, impute_col].isnull()
            num_neighbors -= 1
            id_sort_neighbor += 1
            id_neighbor = sort_df.loc[target_id, id_sort_neighbor]
            r2_neighbor = corr_df.loc[target_id, id_neighbor]

    # Return the results with the impute_col renamed with a leading "imputed_" for clarity
    # return impute_df.rename(columns={c: f"imputed_{c}" for c in impute_df.columns})
    return impute_df[impute_col].rename(f"imputed_{impute_col}")
