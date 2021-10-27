from copy import deepcopy
from splink.default_settings import _normalise_prob_list

from .blocking import block_using_rules
from .gammas import add_gammas
from .maximisation_step import run_maximisation_step
from .model import Model
from .settings import complete_settings_dict
from .vertically_concat import vertically_concatenate_datasets

import warnings

from pyspark.sql.dataframe import DataFrame
from pyspark.sql.session import SparkSession
from pyspark.sql.functions import lit


def _num_target_rows_to_rows_to_sample(target_rows):
    # Number of rows generated by cartesian product is
    # n(n-1)/2, where n is input rows
    # We want to set a target_rows = t, the number of
    # rows generated by Splink and find out how many input rows
    # we need to generate targer rows
    #     Solve t = n(n-1)/2 for n
    #     https://www.wolframalpha.com/input/?i=Solve%5Bt%3Dn+*+%28n+-+1%29+%2F+2%2C+n%5D
    sample_rows = 0.5 * ((8 * target_rows + 1) ** 0.5 + 1)
    return sample_rows


def estimate_u_values(
    settings: dict,
    df_or_dfs: DataFrame,
    spark: SparkSession,
    target_rows: int = 1e6,
    fix_u_probabilities=False,
):
    """Complete the `u_probabilities` section of the settings object
    by directly estimating `u_probabilities` from raw data (i.e. without
    the expectation maximisation algorithm).

    This procedure takes a sample of the data and generates the cartesian
    product of comparisons.  The validity of the u values rests on the
    assumption that the probability of two comparison in the cartesian
    product being a match is very low.  For large datasets, this is typically
    true.

    Args:
        settings (dict): splink settings dictionary
        df_or_dfs (DataFrame or list of DataFrames, optional):
        spark (SparkSession): SparkSession object
        target_rows (int): The number of rows to generate in the cartesian product.
            If set too high, you can run out of memory.  Default value 1e6. Recommend settings to perhaps 1e7.

    Returns:
        dict: The input splink settings dictionary with the `u_probabilities` completed with
              the estimated values
    """

    # Preserve settings as provided
    orig_settings = deepcopy(settings)

    # Do not modify settings object provided by user either
    settings = deepcopy(settings)

    # For the purpoes of estimating u values, we will not use any blocking
    settings["blocking_rules"] = []

    # Minimise the columns that are needed for the calculation
    settings["retain_matching_columns"] = False
    settings["retain_intermediate_calculation_columns"] = False

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        settings = complete_settings_dict(settings, spark)

    if type(df_or_dfs) == DataFrame:
        dfs = [df_or_dfs]
    else:
        dfs = df_or_dfs

    df = vertically_concatenate_datasets(dfs)

    count_rows = df.count()
    if settings["link_type"] in ["dedupe_only", "link_and_dedupe"]:
        sample_size = _num_target_rows_to_rows_to_sample(target_rows)
        proportion = sample_size / count_rows

    if settings["link_type"] == "link_only":
        sample_size = target_rows ** 0.5
        proportion = sample_size / count_rows

    if proportion >= 1.0:
        proportion = 1.0

    df_s = df.sample(False, proportion)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        df_comparison = block_using_rules(settings, df_s, spark)

        df_gammas = add_gammas(df_comparison, settings, spark)

        df_gammas = df_gammas.withColumn("match_probability", lit(0.0))
        df_e_product = df_gammas.withColumn("tf_adjusted_match_prob", lit(0.0))

        model = Model(settings, spark)
        run_maximisation_step(df_e_product, model, spark)
        new_settings = model.current_settings_obj.settings_dict

        for i, col in enumerate(orig_settings["comparison_columns"]):
            u_probs = new_settings["comparison_columns"][i]["u_probabilities"]
            # Ensure non-zero u (https://github.com/moj-analytical-services/splink/issues/161)
            u_probs = [u or 1 / target_rows for u in u_probs]
            u_probs = _normalise_prob_list(u_probs)
            col["u_probabilities"] = u_probs
            if fix_u_probabilities:
                col["fix_u_probabilities"] = True

    return orig_settings