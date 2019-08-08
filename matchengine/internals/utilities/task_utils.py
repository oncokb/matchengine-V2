from __future__ import annotations

import asyncio
import logging
import traceback
from typing import TYPE_CHECKING, List

from pymongo import InsertOne
from pymongo.errors import (
    AutoReconnect,
    CursorNotFound
)

from matchengine.internals.typing.matchengine_types import TrialMatch, IndexUpdateTask, MatchReason, UpdateTask, RunLogUpdateTask

if TYPE_CHECKING:
    from matchengine.internals.engine import MatchEngine

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('matchengine')


async def run_check_indices_task(matchengine: MatchEngine, task, worker_id):
    """
    Ensure indexes exist on collections so queries are performant
    """
    if matchengine.debug:
        log.info(
            f"Worker: {worker_id}, got new CheckIndicesTask")
    try:
        for collection, desired_indices in matchengine.config['indices'].items():
            if collection == "trial_match":
                collection = matchengine.trial_match_collection
            indices = list()
            indices.extend(matchengine.db_ro[collection].list_indexes())
            existing_indices = set()
            for index in indices:
                index_key = list(index['key'].to_dict().keys())[0]
                existing_indices.add(index_key)
            indices_to_create = set(desired_indices) - existing_indices
            for index in indices_to_create:
                matchengine.task_q.put_nowait(IndexUpdateTask(collection, index))
        matchengine.task_q.task_done()
    except Exception as e:
        log.error(f"ERROR: Worker: {worker_id}, error: {e}")
        log.error(f"TRACEBACK: {traceback.print_tb(e.__traceback__)}")
        if isinstance(e, AutoReconnect):
            await matchengine.task_q.put(task)
            matchengine.task_q.task_done()
        elif isinstance(e, CursorNotFound):
            await matchengine.task_q.put(task)
            matchengine.task_q.task_done()
        else:
            matchengine.__exit__(None, None, None)
            matchengine.loop.stop()
            log.error((f"ERROR: Worker: {worker_id}, error: {e}"
                       f"TRACEBACK: {traceback.print_tb(e.__traceback__)}"))
            raise e


async def run_index_update_task(matchengine: MatchEngine, task: IndexUpdateTask, worker_id):
    if matchengine.debug:
        log.info(
            f"Worker: {worker_id}, index {task.index}, collection {task.collection} got new IndexUpdateTask")
    try:
        matchengine.db_rw[task.collection].create_index(task.index)
        matchengine.task_q.task_done()
    except Exception as e:
        log.error(f"ERROR: Worker: {worker_id}, error: {e}")
        log.error(f"TRACEBACK: {traceback.print_tb(e.__traceback__)}")
        if isinstance(e, AutoReconnect):
            matchengine.task_q.put_nowait(task)
            matchengine.task_q.task_done()
        elif isinstance(e, CursorNotFound):
            matchengine.task_q.put_nowait(task)
            matchengine.task_q.task_done()
        else:
            matchengine.loop.stop()
            log.error((f"ERROR: Worker: {worker_id}, error: {e}"
                       f"TRACEBACK: {traceback.print_tb(e.__traceback__)}"))


async def run_query_task(matchengine: MatchEngine, task, worker_id):
    log.info((f"Worker: {worker_id}, protocol_no: {task.trial['protocol_no']} got new QueryTask, "
              f"{matchengine._task_q.qsize()} tasks left in queue"))
    try:
        results: List[MatchReason] = await matchengine.run_query(task.query, task.clinical_ids)
    except Exception as e:
        log.error(f"ERROR: Worker: {worker_id}, error: {e}")
        log.error(f"TRACEBACK: {traceback.print_tb(e.__traceback__)}")
        results = list()
        if isinstance(e, AutoReconnect):
            matchengine.task_q.put_nowait(task)
            matchengine.task_q.task_done()
        elif isinstance(e, CursorNotFound):
            matchengine.task_q.put_nowait(task)
            matchengine.task_q.task_done()
        else:
            matchengine.loop.stop()
            log.error(f"ERROR: Worker: {worker_id}, error: {e}")
            log.error(f"TRACEBACK: {traceback.print_tb(e.__traceback__)}")

    try:
        for result in results:
            matchengine.queue_task_count += 1
            if matchengine.queue_task_count % 1000 == 0:
                log.info(f"Trial match count: {matchengine.queue_task_count}")
            match_document = matchengine.create_trial_matches(TrialMatch(task.trial,
                                                                         task.match_clause_data,
                                                                         task.match_path,
                                                                         task.query,
                                                                         result,
                                                                         matchengine.starttime))
            matchengine.matches[task.trial['protocol_no']][match_document['sample_id']].append(match_document)
    except Exception as e:
        matchengine.loop.stop()
        log.error(f"ERROR: Worker: {worker_id}, error: {e}")
        log.error(f"TRACEBACK: {traceback.print_tb(e.__traceback__)}")
        raise e

    matchengine.task_q.task_done()


async def run_poison_pill(matchengine: MatchEngine, task, worker_id):
    if matchengine.debug:
        log.info(f"Worker: {worker_id} got PoisonPill")
    matchengine.task_q.task_done()


async def run_update_task(matchengine: MatchEngine, task: UpdateTask, worker_id):
    try:
        if matchengine.debug:
            log.info(f"Worker {worker_id} got new UpdateTask {task.protocol_no}")
        await matchengine.async_db_rw[matchengine.trial_match_collection].bulk_write(task.ops, ordered=False)
    except Exception as e:
        log.error(f"ERROR: Worker: {worker_id}, error: {e}")
        log.error(f"TRACEBACK: {traceback.print_tb(e.__traceback__)}")
        if isinstance(e, AutoReconnect):
            matchengine.task_q.task_done()
            matchengine.task_q.put_nowait(task)
        else:
            raise e
    finally:
        matchengine.task_q.task_done()


async def run_run_log_update_task(matchengine: MatchEngine, task: RunLogUpdateTask, worker_id):
    clinical_run_history_collection = f"clinical_run_history_{matchengine.trial_match_collection}"
    run_log_collection = f"run_log_{matchengine.trial_match_collection}"
    try:
        if matchengine.debug:
            log.info(f"Worker {worker_id} got new RunLogUpdateTask {task.protocol_no}")
        dont_need_insert, _ = await asyncio.gather(
            matchengine.async_db_ro.get_collection(clinical_run_history_collection).distinct("clinical_id"),
            matchengine.async_db_rw[run_log_collection].insert_one(matchengine.run_log_entries[task.protocol_no]))
        new_clinical_run_log_docs = set(matchengine.clinical_run_log_entries[task.protocol_no]) - set(dont_need_insert)
        clinical_update_ops = [
            InsertOne({"clinical_id": clinical_id, "run_history": list()})
            for clinical_id
            in new_clinical_run_log_docs
        ]
        if clinical_update_ops:
            await matchengine.async_db_rw.get_collection(
                clinical_run_history_collection
            ).bulk_write(clinical_update_ops, ordered=False)
        await matchengine.async_db_rw.get_collection(clinical_run_history_collection).update_many(
            {'clinical_id': {"$in": list(matchengine.clinical_run_log_entries[task.protocol_no])}},
            {'$push': {"run_history": matchengine.run_id.hex}}
        )
    except Exception as e:
        log.error(f"ERROR: Worker: {worker_id}, error: {e}")
        log.error(f"TRACEBACK: {traceback.print_tb(e.__traceback__)}")
        if isinstance(e, AutoReconnect):
            matchengine.task_q.task_done()
            matchengine.task_q.put_nowait(task)
        else:
            raise e
    finally:
        matchengine.task_q.task_done()