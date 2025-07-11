from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

from splink.internals.splink_dataframe import SplinkDataFrame


def _truncated_edges_sql(
    df_predict: SplinkDataFrame,
    threshold_match_probability: float,
) -> Dict[str, str]:
    """
    Generate SQL to trim edges table to only those with match probability at or
    above a specified threshold
    """
    sql = f"""
        SELECT
            *
        FROM {df_predict.physical_name}
        WHERE match_probability >= {threshold_match_probability}
    """
    truncated_edges_table_name = "__splink__truncated_edges"
    sql_info = {"sql": sql, "output_table_name": truncated_edges_table_name}
    return sql_info


def _node_degree_centralisation_sql(
    df_predict: SplinkDataFrame,
    df_clustered: SplinkDataFrame,
    composite_uid_edges_l: str,
    composite_uid_edges_r: str,
    composite_uid_clusters: str,
    threshold_match_probability: float,
) -> List[Dict[str, str]]:
    """
    Generates sql for computing node degree and node centralisation (i.e.
    normalised node degree) per node, at a given edge threshold.

    This is includes nodes with no edges, as identified via the clusters table.

    Args:
        df_predict (SplinkDataFrame): The results of `linker.inference.predict()`.
        df_clustered (SplinkDataFrame): The outputs of
                `linker.clustering.cluster_pairwise_predictions_at_threshold()`.
        composite_uid_edges_l (str): unique id for left-hand edges.
        composite_uid_edges_r (str): unique id for right-hand edges.
        composite_uid_clusters (str): unique id for clusters.
        threshold_match_probability (float): Filter the pairwise match
            predictions to include only pairwise comparisons with a
            match_probability at or above this threshold.

    Returns:
        array of dicts, with sql string and output name
        for computing cluster size and density
    """
    sqls = []

    sql_info = _truncated_edges_sql(df_predict, threshold_match_probability)
    truncated_edges_table_name = sql_info["output_table_name"]
    sqls.append(sql_info)

    sql = f"""
        SELECT
            {composite_uid_edges_l} AS node,
            {composite_uid_edges_r} AS neighbour
        FROM {truncated_edges_table_name}
            UNION ALL
        SELECT
            {composite_uid_edges_r} AS node,
            {composite_uid_edges_l} AS neighbour
        FROM {truncated_edges_table_name}
    """
    all_nodes_table_name = "__splink__all_nodes"
    sql_info = {"sql": sql, "output_table_name": all_nodes_table_name}
    sqls.append(sql_info)

    # join clusters table to capture edge-less nodes
    # want all clusters included so left join
    sql = f"""
        SELECT
            c.{composite_uid_clusters} AS composite_unique_id,
            c.cluster_id AS cluster_id,
            COUNT(*) FILTER (WHERE n.neighbour IS NOT NULL) AS node_degree,
            COUNT(*) OVER(PARTITION BY c.cluster_id) AS cluster_size
        FROM
            {df_clustered.physical_name} c
        LEFT JOIN
            {all_nodes_table_name} n
        ON
            c.{composite_uid_clusters} = n.node
        GROUP BY composite_unique_id, cluster_id
    """
    node_degree_table_name = "__splink__graph_metrics_node_degree"
    sql_info = {"sql": sql, "output_table_name": node_degree_table_name}
    sqls.append(sql_info)

    # calculate node centrality
    sql = f"""
        SELECT
            composite_unique_id,
            cluster_id,
            node_degree,
            CASE
                WHEN cluster_size > 1 THEN (1.0 * node_degree) / (cluster_size - 1)
                ELSE 0
            END AS node_centrality
        FROM {node_degree_table_name}
    """
    sql_info = {"sql": sql, "output_table_name": "__splink__graph_metrics_nodes"}
    sqls.append(sql_info)

    return sqls


def _node_mapping_table_sql(
    df_node_metrics: SplinkDataFrame,
) -> list[dict[str, str]]:
    """
    Generate SQL to make a table that maps composite_unique_ids to 1-indexed integers.

    This is the node-labelling-scheme required by igraph
    """
    nodes_table_name = df_node_metrics.physical_name
    sql_infos = []
    sql = f"""
        SELECT
            composite_unique_id,
            row_number() OVER(ORDER BY 1) - 1 AS new_id
        FROM
            {nodes_table_name}
    """
    node_mapping_table_name = "__splink__nodes_integer_mapping"
    sql_info = {"sql": sql, "output_table_name": node_mapping_table_name}
    sql_infos.append(sql_info)
    return sql_infos


def _edges_for_igraph_sql(
    df_node_mappings: SplinkDataFrame,
    truncated_edges_table_name: str,
    composite_uid_edges_l: str,
    composite_uid_edges_r: str,
) -> dict[str, str]:
    """
    Generate SQL to relabel an edges table using the node relabelling as
    generated by table specified by `_node_mapping_table_sql`
    """
    node_mapping_table_name = df_node_mappings.physical_name
    sql = f"""
        SELECT
            edges_left_mapped.node_l,
            m.new_id AS node_r
        FROM (
            SELECT
                m.new_id AS node_l,
                e.{composite_uid_edges_r} AS node_r
            FROM
                {truncated_edges_table_name} e
            LEFT JOIN
                {node_mapping_table_name} m
            ON
                e.{composite_uid_edges_l} = m.composite_unique_id
        ) edges_left_mapped
        LEFT JOIN
            {node_mapping_table_name} m
        ON
            m.composite_unique_id = edges_left_mapped.node_r
    """
    edges_with_mapped_ids_table_name = "__splink__edges_with_mapped_ids"
    sql_info = {"sql": sql, "output_table_name": edges_with_mapped_ids_table_name}
    return sql_info


def _bridges_from_igraph_sql(
    df_node_mappings: SplinkDataFrame,
    df_bridges: SplinkDataFrame,
) -> dict[str, str]:
    """
    Generate SQL to relabel an edges table back to the original labelling
    using the node relabelling as generated by table specified by
    `_node_mapping_table_sql` in 'reverse'
    """
    node_mapping_table_name = df_node_mappings.physical_name
    sql = f"""
        SELECT
            e.node_l AS node_l,
            m.composite_unique_id AS node_r,
            TRUE AS is_bridge
        FROM(
            SELECT
                m.composite_unique_id AS node_l,
                e.node_r
            FROM
                {df_bridges.physical_name} e
            LEFT JOIN
                {node_mapping_table_name} m
            ON
                e.node_l = m.new_id
        ) e
        LEFT JOIN
            {node_mapping_table_name} m
        ON
            m.new_id = e.node_r
    """
    edges_with_mapped_ids_table_name = "__splink__bridges_only"
    sql_info = {"sql": sql, "output_table_name": edges_with_mapped_ids_table_name}
    return sql_info


def _full_bridges_sql(
    df_truncated_edges: SplinkDataFrame,
    bridges_only_table_name: str,
    composite_uid_edges_l: str,
    composite_uid_edges_r: str,
) -> dict[str, str]:
    """
    Generate SQL to combine a table of only bridges with the remaining (non-bridge)
    edges, and mark them as such
    """
    sql = f"""
        SELECT
            e.{composite_uid_edges_l} AS composite_unique_id_l,
            e.{composite_uid_edges_r} AS composite_unique_id_r,
            COALESCE(b.is_bridge, FALSE) AS is_bridge
        FROM
        {df_truncated_edges.physical_name} e
        LEFT JOIN
        {bridges_only_table_name} b
        ON
            e.{composite_uid_edges_l} = b.node_l
        AND e.{composite_uid_edges_r} = b.node_r
    """
    edges_metrics_table_name = "__splink__graph_metrics_edges"
    sql_info = {"sql": sql, "output_table_name": edges_metrics_table_name}
    return sql_info


def _basic_edge_metrics_sql(
    composite_uid_edges_l: str,
    composite_uid_edges_r: str,
    truncated_edges_table_name: str,
) -> Dict[str, str]:
    # dummy sql that returns the edges without any metrics, as there are none
    # that we can currently compute without igraph
    sql_info = {
        "sql": (
            f"SELECT {composite_uid_edges_l} AS composite_unique_id_l, "
            f"{composite_uid_edges_r} AS composite_unique_id_r "
            f"FROM {truncated_edges_table_name}"
        ),
        "output_table_name": "__splink__graph_metrics_edges",
    }
    return sql_info


def _size_density_centralisation_sql(
    df_node_metrics: SplinkDataFrame,
) -> List[Dict[str, str]]:
    """
    Generates sql for computing cluster size, density and cluster centralisation.

    The frame df_node_metrics already encodes the relevant information about edges
    and nodes for a given choice of threshold probability.

    Args:
        df_node_metrics (SplinkDataFrame): The results of
            `linker._compute_metrics_nodes()`.

    Returns:
        array of dicts, with sql string and output name
        for computing cluster size and density
    """

    sqls = []
    # Count nodes and edges per cluster
    sql = f"""
        SELECT
            cluster_id,
            COUNT(*) AS n_nodes,
            SUM(node_degree)/2.0 AS n_edges,
            CASE
                WHEN COUNT(*) > 2 THEN
                    1.0*(COUNT(*) * MAX(node_degree) -  SUM(node_degree)) /
                    ((COUNT(*) - 1) * (COUNT(*) - 2))
                ELSE
                    NULL
            END AS cluster_centralisation
        FROM {df_node_metrics.physical_name}
        GROUP BY
            cluster_id
    """
    sql_info = {"sql": sql, "output_table_name": "__splink__counts_per_cluster"}
    sqls.append(sql_info)

    # Compute density of each cluster
    sql = """
        SELECT
            cluster_id,
            n_nodes,
            n_edges,
            CASE
                WHEN n_nodes > 1 THEN
                    1.0*(n_edges * 2)/(n_nodes * (n_nodes-1))
                ELSE
                    -- n_nodes is 1 (or 0) density undefined
                    NULL
            END AS density,
            cluster_centralisation
        FROM __splink__counts_per_cluster
    """
    sql_info = {"sql": sql, "output_table_name": "__splink__graph_metrics_clusters"}
    sqls.append(sql_info)

    return sqls


@dataclass
class GraphMetricsResults:
    nodes: SplinkDataFrame
    edges: SplinkDataFrame
    clusters: SplinkDataFrame

    def __repr__(self):
        msg = (
            "A data class of Splink dataframes containing metrics for nodes, edges "
            "and clusters.\n"
            "\nAccess dataframes via attributes:\n"
            "`compute_graph_metrics.nodes` for node metrics,\n"
            "`compute_graph_metrics.edges` for edge metrics, and\n"
            "`compute_graph_metrics.clusters` for cluster metrics\n"
        )
        return msg
