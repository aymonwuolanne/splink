from __future__ import annotations

import logging
import time
from typing import List, Optional

from splink.internals.database_api import DatabaseAPISubClass
from splink.internals.pipeline import CTEPipeline
from splink.internals.splink_dataframe import SplinkDataFrame

logger = logging.getLogger(__name__)

def one_to_one_clustering(
    nodes_table: SplinkDataFrame,
    edges_table: SplinkDataFrame,
    node_id_column_name: str,
    source_dataset_column_name: str,
    edge_id_column_name_left: str,
    edge_id_column_name_right: str,
    source_datasets: List[str],
    db_api: DatabaseAPISubClass,
    threshold_match_probability: Optional[float],
) -> SplinkDataFrame:
    """One to one clustering algorithm.

    This function clusters together records so that at most one record from each dataset is in each cluster.

    Args:
        
    Returns:
        SplinkDataFrame: A dataframe containing the connected components list
        for your link or dedupe job.

    """

    pipeline = CTEPipeline([edges_table])

    match_prob_expr = f"where match_probability >= {threshold_match_probability}"
    if threshold_match_probability is None:
        match_prob_expr = ""

    # Add 'reverse-edges' so that the algorithm can rank all incoming and outgoing edges
    sql = f"""
    select
        {edge_id_column_name_left} as node_id,
        {edge_id_column_name_right} as neighbour,
        match_probability
    from {edges_table.templated_name}
    {match_prob_expr}

    UNION ALL

    select
    {edge_id_column_name_right} as node_id,
    {edge_id_column_name_left} as neighbour,
    match_probability
    from {edges_table.templated_name}
    {match_prob_expr}
    """
    pipeline.enqueue_sql(sql, "__splink__df_neighbours")

    neighbours = db_api.sql_pipeline_to_splink_dataframe(pipeline)

    pipeline = CTEPipeline([nodes_table])

    sql = f"""
    select
        {node_id_column_name} as node_id,
        {node_id_column_name} as representative,
        {source_dataset_column_name} as source_dataset
    from {nodes_table.templated_name}
    """

    pipeline.enqueue_sql(sql, "__splink__df_representatives")

    prev_representatives = db_api.sql_pipeline_to_splink_dataframe(pipeline)

    iteration, needs_updating_count = 0, 1
    while needs_updating_count > 0:
        start_time = time.time()
        iteration += 1

        pipeline = CTEPipeline([neighbours, prev_representatives])

        # might need to quote the value here? 
        contains_expr = ", ".join([f"max(source_dataset == '{sd}') as contains_{sd}" for sd in source_datasets])
        
        sql = f"""
        select
            representative,
            {contains_expr}
        from {prev_representatives.physical_name}
        group by representative
        """

        pipeline.enqueue_sql(sql, f"__splink__representative_contains_flags_{iteration}")

        sql = f"""
        select
            r.node_id,
            r.source_dataset,
            cf.*
        from {prev_representatives.physical_name} as r 
        inner join __splink__representative_contains_flags_{iteration} as cf
        on r.representative = cf.representative
        """

        pipeline.enqueue_sql(sql, f"__splink__df_representatives_with_flags_{iteration}")

        duplicate_criteria = " or ".join([f"(l.contains_{sd} and r.contains_{sd})" for sd in source_datasets])
    
        # must be calculated every iteration since the where condition changes as the clustering progresses
        sql = f"""
        select 
            neighbours.node_id,
            neighbours.neighbour,
            {duplicate_criteria} as duplicate_criteria,
            row_number() over (partition by l.representative order by match_probability desc) as rank_l,
            row_number() over (partition by r.representative order by match_probability desc) as rank_r,
        from {neighbours.physical_name} as neighbours
        inner join __splink__df_representatives_with_flags_{iteration} as l
        on neighbours.node_id = l.node_id
        inner join __splink__df_representatives_with_flags_{iteration} as r 
        on neighbours.neighbour = r.node_id
        where l.representative <> r.representative
        """
        
        # note for the future: a strategy to handle ties would go right here. 
        
        pipeline.enqueue_sql(sql, f"__splink__df_ranked_{iteration}")
    
        sql = f"""
        select
            node_id,
            neighbour
        from __splink__df_ranked_{iteration} 
        where rank_l = 1 and rank_r = 1 and not duplicate_criteria
        """

        pipeline.enqueue_sql(sql, f"__splink__df_neighbours_{iteration}")
    
        sql = f"""
        select
        source.node_id, 
        min(source.representative) as representative
        from
        (
            select
                neighbours.node_id,
                repr_neighbour.representative as representative,
            from __splink__df_neighbours_{iteration} as neighbours
            left join {prev_representatives.physical_name} as repr_neighbour
            on neighbours.neighbour = repr_neighbour.node_id
            
            union all
            
            select
                node_id,
                representative
            from {prev_representatives.physical_name}
        ) AS source
        group by source.node_id
        """

        pipeline.enqueue_sql(sql, f"r")
    
        sql = f"""
        select
            r.node_id,
            r.representative,
            repr.source_dataset,
            r.representative <> repr.representative as needs_updating
        from r
        inner join {prev_representatives.physical_name} as repr
        on r.node_id = repr.node_id
        """

        pipeline.enqueue_sql(sql, f"__splink__df_representatives_{iteration}")

        representatives = db_api.sql_pipeline_to_splink_dataframe(pipeline)

        prev_representatives.drop_table_from_database_and_remove_from_cache()
        prev_representatives = representatives

        pipeline = CTEPipeline()

        # assess if the exit condition has been met
        sql = f"""
        select 
            count(*) as count_of_nodes_needing_updating
        from {representatives.physical_name}
        where needs_updating
        """

        pipeline.enqueue_sql(sql, "__splink__df_root_rows")

        root_rows_df = db_api.sql_pipeline_to_splink_dataframe(
            pipeline, use_cache=False
        )

        root_rows = root_rows_df.as_record_dict()
        root_rows_df.drop_table_from_database_and_remove_from_cache()
        needs_updating_count = root_rows[0]["count_of_nodes_needing_updating"]
        logger.info(
            f"Completed iteration {iteration}, "
            f"num representatives needing updating: {needs_updating_count}"
        )
        end_time = time.time()
        logger.log(15, f"    Iteration time: {end_time - start_time} seconds")

    pipeline = CTEPipeline()

    sql = f"""
    select node_id as {node_id_column_name}, representative as cluster_id 
    from {representatives.physical_name}
    """ 

    pipeline.enqueue_sql(sql, "__splink__clustering_output_final")

    final_result = db_api.sql_pipeline_to_splink_dataframe(pipeline)

    representatives.drop_table_from_database_and_remove_from_cache()
    neighbours.drop_table_from_database_and_remove_from_cache()

    return final_result