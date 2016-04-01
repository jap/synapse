# -*- coding: utf-8 -*-
# Copyright 2014-2016 OpenMarket Ltd
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from ._base import SQLBaseStore, _RollbackButIsFineException

from twisted.internet import defer, reactor

from synapse.events import FrozenEvent, USE_FROZEN_DICTS
from synapse.events.utils import prune_event

from synapse.util.logcontext import preserve_fn, PreserveLoggingContext
from synapse.util.logutils import log_function
from synapse.api.constants import EventTypes

from canonicaljson import encode_canonical_json
from collections import namedtuple

import logging
import math
import ujson as json

logger = logging.getLogger(__name__)


def encode_json(json_object):
    if USE_FROZEN_DICTS:
        # ujson doesn't like frozen_dicts
        return encode_canonical_json(json_object)
    else:
        return json.dumps(json_object, ensure_ascii=False)

# These values are used in the `enqueus_event` and `_do_fetch` methods to
# control how we batch/bulk fetch events from the database.
# The values are plucked out of thing air to make initial sync run faster
# on jki.re
# TODO: Make these configurable.
EVENT_QUEUE_THREADS = 3  # Max number of threads that will fetch events
EVENT_QUEUE_ITERATIONS = 3  # No. times we block waiting for requests for events
EVENT_QUEUE_TIMEOUT_S = 0.1  # Timeout when waiting for requests for events


class EventsStore(SQLBaseStore):
    EVENT_ORIGIN_SERVER_TS_NAME = "event_origin_server_ts"

    def __init__(self, hs):
        super(EventsStore, self).__init__(hs)
        self.register_background_update_handler(
            self.EVENT_ORIGIN_SERVER_TS_NAME, self._background_reindex_origin_server_ts
        )

    @defer.inlineCallbacks
    def persist_events(self, events_and_contexts, backfilled=False):
        if not events_and_contexts:
            return

        if backfilled:
            stream_ordering_manager = self._backfill_id_gen.get_next_mult(
                len(events_and_contexts)
            )
        else:
            stream_ordering_manager = self._stream_id_gen.get_next_mult(
                len(events_and_contexts)
            )

        state_group_id_manager = self._state_groups_id_gen.get_next_mult(
            len(events_and_contexts)
        )
        with stream_ordering_manager as stream_orderings:
            with state_group_id_manager as state_group_ids:
                for (event, context), stream, state_group_id in zip(
                    events_and_contexts, stream_orderings, state_group_ids
                ):
                    event.internal_metadata.stream_ordering = stream
                    # Assign a state group_id in case a new id is needed for
                    # this context. In theory we only need to assign this
                    # for contexts that have current_state and aren't outliers
                    # but that make the code more complicated. Assigning an ID
                    # per event only causes the state_group_ids to grow as fast
                    # as the stream_ordering so in practise shouldn't be a problem.
                    context.new_state_group_id = state_group_id

                chunks = [
                    events_and_contexts[x:x + 100]
                    for x in xrange(0, len(events_and_contexts), 100)
                ]

                for chunk in chunks:
                    # We can't easily parallelize these since different chunks
                    # might contain the same event. :(
                    yield self.runInteraction(
                        "persist_events",
                        self._persist_events_txn,
                        events_and_contexts=chunk,
                        backfilled=backfilled,
                    )

    @defer.inlineCallbacks
    @log_function
    def persist_event(self, event, context, current_state=None):

        try:
            with self._stream_id_gen.get_next() as stream_ordering:
                with self._state_groups_id_gen.get_next() as state_group_id:
                    event.internal_metadata.stream_ordering = stream_ordering
                    context.new_state_group_id = state_group_id
                    yield self.runInteraction(
                        "persist_event",
                        self._persist_event_txn,
                        event=event,
                        context=context,
                        current_state=current_state,
                    )
        except _RollbackButIsFineException:
            pass

        max_persisted_id = yield self._stream_id_gen.get_current_token()
        defer.returnValue((stream_ordering, max_persisted_id))

    @defer.inlineCallbacks
    def get_event(self, event_id, check_redacted=True,
                  get_prev_content=False, allow_rejected=False,
                  allow_none=False):
        """Get an event from the database by event_id.

        Args:
            event_id (str): The event_id of the event to fetch
            check_redacted (bool): If True, check if event has been redacted
                and redact it.
            get_prev_content (bool): If True and event is a state event,
                include the previous states content in the unsigned field.
            allow_rejected (bool): If True return rejected events.
            allow_none (bool): If True, return None if no event found, if
                False throw an exception.

        Returns:
            Deferred : A FrozenEvent.
        """
        events = yield self._get_events(
            [event_id],
            check_redacted=check_redacted,
            get_prev_content=get_prev_content,
            allow_rejected=allow_rejected,
        )

        if not events and not allow_none:
            raise RuntimeError("Could not find event %s" % (event_id,))

        defer.returnValue(events[0] if events else None)

    @defer.inlineCallbacks
    def get_events(self, event_ids, check_redacted=True,
                   get_prev_content=False, allow_rejected=False):
        """Get events from the database

        Args:
            event_ids (list): The event_ids of the events to fetch
            check_redacted (bool): If True, check if event has been redacted
                and redact it.
            get_prev_content (bool): If True and event is a state event,
                include the previous states content in the unsigned field.
            allow_rejected (bool): If True return rejected events.

        Returns:
            Deferred : Dict from event_id to event.
        """
        events = yield self._get_events(
            event_ids,
            check_redacted=check_redacted,
            get_prev_content=get_prev_content,
            allow_rejected=allow_rejected,
        )

        defer.returnValue({e.event_id: e for e in events})

    @log_function
    def _persist_event_txn(self, txn, event, context, current_state):
        # We purposefully do this first since if we include a `current_state`
        # key, we *want* to update the `current_state_events` table
        if current_state:
            txn.call_after(self._get_current_state_for_key.invalidate_all)
            txn.call_after(self.get_rooms_for_user.invalidate_all)
            txn.call_after(self.get_users_in_room.invalidate, (event.room_id,))
            txn.call_after(self.get_joined_hosts_for_room.invalidate, (event.room_id,))
            txn.call_after(self.get_room_name_and_aliases.invalidate, (event.room_id,))

            # Add an entry to the current_state_resets table to record the point
            # where we clobbered the current state
            stream_order = event.internal_metadata.stream_ordering
            self._simple_insert_txn(
                txn,
                table="current_state_resets",
                values={"event_stream_ordering": stream_order}
            )

            self._simple_delete_txn(
                txn,
                table="current_state_events",
                keyvalues={"room_id": event.room_id},
            )

            for s in current_state:
                self._simple_insert_txn(
                    txn,
                    "current_state_events",
                    {
                        "event_id": s.event_id,
                        "room_id": s.room_id,
                        "type": s.type,
                        "state_key": s.state_key,
                    }
                )

        return self._persist_events_txn(
            txn,
            [(event, context)],
            backfilled=False,
        )

    @log_function
    def _persist_events_txn(self, txn, events_and_contexts, backfilled):
        depth_updates = {}
        for event, context in events_and_contexts:
            # Remove the any existing cache entries for the event_ids
            txn.call_after(self._invalidate_get_event_cache, event.event_id)
            if not backfilled:
                txn.call_after(
                    self._events_stream_cache.entity_has_changed,
                    event.room_id, event.internal_metadata.stream_ordering,
                )

            if not event.internal_metadata.is_outlier():
                depth_updates[event.room_id] = max(
                    event.depth, depth_updates.get(event.room_id, event.depth)
                )

            if context.push_actions:
                self._set_push_actions_for_event_and_users_txn(
                    txn, event, context.push_actions
                )

        if event.type == EventTypes.Redaction and event.redacts is not None:
            self._remove_push_actions_for_event_id_txn(
                txn, event.room_id, event.redacts
            )

        for room_id, depth in depth_updates.items():
            self._update_min_depth_for_room_txn(txn, room_id, depth)

        txn.execute(
            "SELECT event_id, outlier FROM events WHERE event_id in (%s)" % (
                ",".join(["?"] * len(events_and_contexts)),
            ),
            [event.event_id for event, _ in events_and_contexts]
        )
        have_persisted = {
            event_id: outlier
            for event_id, outlier in txn.fetchall()
        }

        event_map = {}
        to_remove = set()
        for event, context in events_and_contexts:
            # Handle the case of the list including the same event multiple
            # times. The tricky thing here is when they differ by whether
            # they are an outlier.
            if event.event_id in event_map:
                other = event_map[event.event_id]

                if not other.internal_metadata.is_outlier():
                    to_remove.add(event)
                    continue
                elif not event.internal_metadata.is_outlier():
                    to_remove.add(event)
                    continue
                else:
                    to_remove.add(other)

            event_map[event.event_id] = event

            if event.event_id not in have_persisted:
                continue

            to_remove.add(event)

            outlier_persisted = have_persisted[event.event_id]
            if not event.internal_metadata.is_outlier() and outlier_persisted:
                self._store_mult_state_groups_txn(txn, ((event, context),))

                metadata_json = encode_json(
                    event.internal_metadata.get_dict()
                ).decode("UTF-8")

                sql = (
                    "UPDATE event_json SET internal_metadata = ?"
                    " WHERE event_id = ?"
                )
                txn.execute(
                    sql,
                    (metadata_json, event.event_id,)
                )

                stream_order = event.internal_metadata.stream_ordering
                state_group_id = context.state_group or context.new_state_group_id
                self._simple_insert_txn(
                    txn,
                    table="ex_outlier_stream",
                    values={
                        "event_stream_ordering": stream_order,
                        "event_id": event.event_id,
                        "state_group": state_group_id,
                    }
                )

                sql = (
                    "UPDATE events SET outlier = ?"
                    " WHERE event_id = ?"
                )
                txn.execute(
                    sql,
                    (False, event.event_id,)
                )

                self._update_extremeties(txn, [event])

        events_and_contexts = [
            ec for ec in events_and_contexts if ec[0] not in to_remove
        ]

        if not events_and_contexts:
            return

        self._store_mult_state_groups_txn(txn, events_and_contexts)

        self._handle_mult_prev_events(
            txn,
            events=[event for event, _ in events_and_contexts],
        )

        for event, _ in events_and_contexts:
            if event.type == EventTypes.Name:
                self._store_room_name_txn(txn, event)
            elif event.type == EventTypes.Topic:
                self._store_room_topic_txn(txn, event)
            elif event.type == EventTypes.Message:
                self._store_room_message_txn(txn, event)
            elif event.type == EventTypes.Redaction:
                self._store_redaction(txn, event)
            elif event.type == EventTypes.RoomHistoryVisibility:
                self._store_history_visibility_txn(txn, event)
            elif event.type == EventTypes.GuestAccess:
                self._store_guest_access_txn(txn, event)

        self._store_room_members_txn(
            txn,
            [
                event
                for event, _ in events_and_contexts
                if event.type == EventTypes.Member
            ]
        )

        def event_dict(event):
            return {
                k: v
                for k, v in event.get_dict().items()
                if k not in [
                    "redacted",
                    "redacted_because",
                ]
            }

        self._simple_insert_many_txn(
            txn,
            table="event_json",
            values=[
                {
                    "event_id": event.event_id,
                    "room_id": event.room_id,
                    "internal_metadata": encode_json(
                        event.internal_metadata.get_dict()
                    ).decode("UTF-8"),
                    "json": encode_json(event_dict(event)).decode("UTF-8"),
                }
                for event, _ in events_and_contexts
            ],
        )

        self._simple_insert_many_txn(
            txn,
            table="events",
            values=[
                {
                    "stream_ordering": event.internal_metadata.stream_ordering,
                    "topological_ordering": event.depth,
                    "depth": event.depth,
                    "event_id": event.event_id,
                    "room_id": event.room_id,
                    "type": event.type,
                    "processed": True,
                    "outlier": event.internal_metadata.is_outlier(),
                    "content": encode_json(event.content).decode("UTF-8"),
                    "origin_server_ts": int(event.origin_server_ts),
                }
                for event, _ in events_and_contexts
            ],
        )

        if context.rejected:
            self._store_rejections_txn(
                txn, event.event_id, context.rejected
            )

        self._simple_insert_many_txn(
            txn,
            table="event_auth",
            values=[
                {
                    "event_id": event.event_id,
                    "room_id": event.room_id,
                    "auth_id": auth_id,
                }
                for event, _ in events_and_contexts
                for auth_id, _ in event.auth_events
            ],
        )

        self._store_event_reference_hashes_txn(
            txn, [event for event, _ in events_and_contexts]
        )

        state_events_and_contexts = [
            ec for ec in events_and_contexts if ec[0].is_state()
        ]

        state_values = []
        for event, context in state_events_and_contexts:
            vals = {
                "event_id": event.event_id,
                "room_id": event.room_id,
                "type": event.type,
                "state_key": event.state_key,
            }

            # TODO: How does this work with backfilling?
            if hasattr(event, "replaces_state"):
                vals["prev_state"] = event.replaces_state

            state_values.append(vals)

        self._simple_insert_many_txn(
            txn,
            table="state_events",
            values=state_values,
        )

        self._simple_insert_many_txn(
            txn,
            table="event_edges",
            values=[
                {
                    "event_id": event.event_id,
                    "prev_event_id": prev_id,
                    "room_id": event.room_id,
                    "is_state": True,
                }
                for event, _ in state_events_and_contexts
                for prev_id, _ in event.prev_state
            ],
        )

        if backfilled:
            # Backfilled events come before the current state so we don't need
            # to update the current state table
            return

        for event, _ in state_events_and_contexts:
            if (not event.internal_metadata.is_invite_from_remote()
                    and event.internal_metadata.is_outlier()):
                # Outlier events generally shouldn't clobber the current state.
                # However invites from remote severs for rooms we aren't in
                # are a bit special: they don't come with any associated
                # state so are technically an outlier, however all the
                # client-facing code assumes that they are in the current
                # state table so we insert the event anyway.
                continue

            if context.rejected:
                # If the event failed it's auth checks then it shouldn't
                # clobbler the current state.
                continue

            txn.call_after(
                self._get_current_state_for_key.invalidate,
                (event.room_id, event.type, event.state_key,)
            )

            if event.type in [EventTypes.Name, EventTypes.Aliases]:
                txn.call_after(
                    self.get_room_name_and_aliases.invalidate,
                    (event.room_id,)
                )

            self._simple_upsert_txn(
                txn,
                "current_state_events",
                keyvalues={
                    "room_id": event.room_id,
                    "type": event.type,
                    "state_key": event.state_key,
                },
                values={
                    "event_id": event.event_id,
                }
            )

        return

    def _store_redaction(self, txn, event):
        # invalidate the cache for the redacted event
        txn.call_after(self._invalidate_get_event_cache, event.redacts)
        txn.execute(
            "INSERT INTO redactions (event_id, redacts) VALUES (?,?)",
            (event.event_id, event.redacts)
        )

    def have_events(self, event_ids):
        """Given a list of event ids, check if we have already processed them.

        Returns:
            dict: Has an entry for each event id we already have seen. Maps to
            the rejected reason string if we rejected the event, else maps to
            None.
        """
        if not event_ids:
            return defer.succeed({})

        def f(txn):
            sql = (
                "SELECT e.event_id, reason FROM events as e "
                "LEFT JOIN rejections as r ON e.event_id = r.event_id "
                "WHERE e.event_id = ?"
            )

            res = {}
            for event_id in event_ids:
                txn.execute(sql, (event_id,))
                row = txn.fetchone()
                if row:
                    _, rejected = row
                    res[event_id] = rejected

            return res

        return self.runInteraction(
            "have_events", f,
        )

    @defer.inlineCallbacks
    def _get_events(self, event_ids, check_redacted=True,
                    get_prev_content=False, allow_rejected=False):
        if not event_ids:
            defer.returnValue([])

        event_id_list = event_ids
        event_ids = set(event_ids)

        event_map = self._get_events_from_cache(
            event_ids,
            check_redacted=check_redacted,
            get_prev_content=get_prev_content,
            allow_rejected=allow_rejected,
        )

        missing_events_ids = [e for e in event_ids if e not in event_map]

        if missing_events_ids:
            missing_events = yield self._enqueue_events(
                missing_events_ids,
                check_redacted=check_redacted,
                get_prev_content=get_prev_content,
                allow_rejected=allow_rejected,
            )

            event_map.update(missing_events)

        defer.returnValue([
            event_map[e_id] for e_id in event_id_list
            if e_id in event_map and event_map[e_id]
        ])

    def _get_events_txn(self, txn, event_ids, check_redacted=True,
                        get_prev_content=False, allow_rejected=False):
        if not event_ids:
            return []

        event_map = self._get_events_from_cache(
            event_ids,
            check_redacted=check_redacted,
            get_prev_content=get_prev_content,
            allow_rejected=allow_rejected,
        )

        missing_events_ids = [e for e in event_ids if e not in event_map]

        if not missing_events_ids:
            return [
                event_map[e_id] for e_id in event_ids
                if e_id in event_map and event_map[e_id]
            ]

        missing_events = self._fetch_events_txn(
            txn,
            missing_events_ids,
            check_redacted=check_redacted,
            get_prev_content=get_prev_content,
            allow_rejected=allow_rejected,
        )

        event_map.update(missing_events)

        return [
            event_map[e_id] for e_id in event_ids
            if e_id in event_map and event_map[e_id]
        ]

    def _invalidate_get_event_cache(self, event_id):
        for check_redacted in (False, True):
            for get_prev_content in (False, True):
                self._get_event_cache.invalidate(
                    (event_id, check_redacted, get_prev_content)
                )

    def _get_event_txn(self, txn, event_id, check_redacted=True,
                       get_prev_content=False, allow_rejected=False):

        events = self._get_events_txn(
            txn, [event_id],
            check_redacted=check_redacted,
            get_prev_content=get_prev_content,
            allow_rejected=allow_rejected,
        )

        return events[0] if events else None

    def _get_events_from_cache(self, events, check_redacted, get_prev_content,
                               allow_rejected):
        event_map = {}

        for event_id in events:
            try:
                ret = self._get_event_cache.get(
                    (event_id, check_redacted, get_prev_content,)
                )

                if allow_rejected or not ret.rejected_reason:
                    event_map[event_id] = ret
                else:
                    event_map[event_id] = None
            except KeyError:
                pass

        return event_map

    def _do_fetch(self, conn):
        """Takes a database connection and waits for requests for events from
        the _event_fetch_list queue.
        """
        event_list = []
        i = 0
        while True:
            try:
                with self._event_fetch_lock:
                    event_list = self._event_fetch_list
                    self._event_fetch_list = []

                    if not event_list:
                        single_threaded = self.database_engine.single_threaded
                        if single_threaded or i > EVENT_QUEUE_ITERATIONS:
                            self._event_fetch_ongoing -= 1
                            return
                        else:
                            self._event_fetch_lock.wait(EVENT_QUEUE_TIMEOUT_S)
                            i += 1
                            continue
                    i = 0

                event_id_lists = zip(*event_list)[0]
                event_ids = [
                    item for sublist in event_id_lists for item in sublist
                ]

                rows = self._new_transaction(
                    conn, "do_fetch", [], None, self._fetch_event_rows, event_ids
                )

                row_dict = {
                    r["event_id"]: r
                    for r in rows
                }

                # We only want to resolve deferreds from the main thread
                def fire(lst, res):
                    for ids, d in lst:
                        if not d.called:
                            try:
                                with PreserveLoggingContext():
                                    d.callback([
                                        res[i]
                                        for i in ids
                                        if i in res
                                    ])
                            except:
                                logger.exception("Failed to callback")
                with PreserveLoggingContext():
                    reactor.callFromThread(fire, event_list, row_dict)
            except Exception as e:
                logger.exception("do_fetch")

                # We only want to resolve deferreds from the main thread
                def fire(evs):
                    for _, d in evs:
                        if not d.called:
                            with PreserveLoggingContext():
                                d.errback(e)

                if event_list:
                    with PreserveLoggingContext():
                        reactor.callFromThread(fire, event_list)

    @defer.inlineCallbacks
    def _enqueue_events(self, events, check_redacted=True,
                        get_prev_content=False, allow_rejected=False):
        """Fetches events from the database using the _event_fetch_list. This
        allows batch and bulk fetching of events - it allows us to fetch events
        without having to create a new transaction for each request for events.
        """
        if not events:
            defer.returnValue({})

        events_d = defer.Deferred()
        with self._event_fetch_lock:
            self._event_fetch_list.append(
                (events, events_d)
            )

            self._event_fetch_lock.notify()

            if self._event_fetch_ongoing < EVENT_QUEUE_THREADS:
                self._event_fetch_ongoing += 1
                should_start = True
            else:
                should_start = False

        if should_start:
            with PreserveLoggingContext():
                self.runWithConnection(
                    self._do_fetch
                )

        with PreserveLoggingContext():
            rows = yield events_d

        if not allow_rejected:
            rows[:] = [r for r in rows if not r["rejects"]]

        res = yield defer.gatherResults(
            [
                preserve_fn(self._get_event_from_row)(
                    row["internal_metadata"], row["json"], row["redacts"],
                    check_redacted=check_redacted,
                    get_prev_content=get_prev_content,
                    rejected_reason=row["rejects"],
                )
                for row in rows
            ],
            consumeErrors=True
        )

        defer.returnValue({
            e.event_id: e
            for e in res if e
        })

    def _fetch_event_rows(self, txn, events):
        rows = []
        N = 200
        for i in range(1 + len(events) / N):
            evs = events[i * N:(i + 1) * N]
            if not evs:
                break

            sql = (
                "SELECT "
                " e.event_id as event_id, "
                " e.internal_metadata,"
                " e.json,"
                " r.redacts as redacts,"
                " rej.event_id as rejects "
                " FROM event_json as e"
                " LEFT JOIN rejections as rej USING (event_id)"
                " LEFT JOIN redactions as r ON e.event_id = r.redacts"
                " WHERE e.event_id IN (%s)"
            ) % (",".join(["?"] * len(evs)),)

            txn.execute(sql, evs)
            rows.extend(self.cursor_to_dict(txn))

        return rows

    def _fetch_events_txn(self, txn, events, check_redacted=True,
                          get_prev_content=False, allow_rejected=False):
        if not events:
            return {}

        rows = self._fetch_event_rows(
            txn, events,
        )

        if not allow_rejected:
            rows[:] = [r for r in rows if not r["rejects"]]

        res = [
            self._get_event_from_row_txn(
                txn,
                row["internal_metadata"], row["json"], row["redacts"],
                check_redacted=check_redacted,
                get_prev_content=get_prev_content,
                rejected_reason=row["rejects"],
            )
            for row in rows
        ]

        return {
            r.event_id: r
            for r in res
        }

    @defer.inlineCallbacks
    def _get_event_from_row(self, internal_metadata, js, redacted,
                            check_redacted=True, get_prev_content=False,
                            rejected_reason=None):
        d = json.loads(js)
        internal_metadata = json.loads(internal_metadata)

        if rejected_reason:
            rejected_reason = yield self._simple_select_one_onecol(
                table="rejections",
                keyvalues={"event_id": rejected_reason},
                retcol="reason",
                desc="_get_event_from_row",
            )

        ev = FrozenEvent(
            d,
            internal_metadata_dict=internal_metadata,
            rejected_reason=rejected_reason,
        )

        if check_redacted and redacted:
            ev = prune_event(ev)

            redaction_id = yield self._simple_select_one_onecol(
                table="redactions",
                keyvalues={"redacts": ev.event_id},
                retcol="event_id",
                desc="_get_event_from_row",
            )

            ev.unsigned["redacted_by"] = redaction_id
            # Get the redaction event.

            because = yield self.get_event(
                redaction_id,
                check_redacted=False,
                allow_none=True,
            )

            if because:
                # It's fine to do add the event directly, since get_pdu_json
                # will serialise this field correctly
                ev.unsigned["redacted_because"] = because

        if get_prev_content and "replaces_state" in ev.unsigned:
            prev = yield self.get_event(
                ev.unsigned["replaces_state"],
                get_prev_content=False,
                allow_none=True,
            )
            if prev:
                ev.unsigned["prev_content"] = prev.content
                ev.unsigned["prev_sender"] = prev.sender

        self._get_event_cache.prefill(
            (ev.event_id, check_redacted, get_prev_content), ev
        )

        defer.returnValue(ev)

    def _get_event_from_row_txn(self, txn, internal_metadata, js, redacted,
                                check_redacted=True, get_prev_content=False,
                                rejected_reason=None):
        d = json.loads(js)
        internal_metadata = json.loads(internal_metadata)

        if rejected_reason:
            rejected_reason = self._simple_select_one_onecol_txn(
                txn,
                table="rejections",
                keyvalues={"event_id": rejected_reason},
                retcol="reason",
            )

        ev = FrozenEvent(
            d,
            internal_metadata_dict=internal_metadata,
            rejected_reason=rejected_reason,
        )

        if check_redacted and redacted:
            ev = prune_event(ev)

            redaction_id = self._simple_select_one_onecol_txn(
                txn,
                table="redactions",
                keyvalues={"redacts": ev.event_id},
                retcol="event_id",
            )

            ev.unsigned["redacted_by"] = redaction_id
            # Get the redaction event.

            because = self._get_event_txn(
                txn,
                redaction_id,
                check_redacted=False
            )

            if because:
                ev.unsigned["redacted_because"] = because

        if get_prev_content and "replaces_state" in ev.unsigned:
            prev = self._get_event_txn(
                txn,
                ev.unsigned["replaces_state"],
                get_prev_content=False,
            )
            if prev:
                ev.unsigned["prev_content"] = prev.content
                ev.unsigned["prev_sender"] = prev.sender

        self._get_event_cache.prefill(
            (ev.event_id, check_redacted, get_prev_content), ev
        )

        return ev

    def _parse_events_txn(self, txn, rows):
        event_ids = [r["event_id"] for r in rows]

        return self._get_events_txn(txn, event_ids)

    @defer.inlineCallbacks
    def count_daily_messages(self):
        """
        Returns an estimate of the number of messages sent in the last day.

        If it has been significantly less or more than one day since the last
        call to this function, it will return None.
        """
        def _count_messages(txn):
            now = self.hs.get_clock().time()

            txn.execute(
                "SELECT reported_stream_token, reported_time FROM stats_reporting"
            )
            last_reported = self.cursor_to_dict(txn)

            txn.execute(
                "SELECT stream_ordering"
                " FROM events"
                " ORDER BY stream_ordering DESC"
                " LIMIT 1"
            )
            now_reporting = self.cursor_to_dict(txn)
            if not now_reporting:
                logger.info("Calculating daily messages skipped; no now_reporting")
                return None
            now_reporting = now_reporting[0]["stream_ordering"]

            txn.execute("DELETE FROM stats_reporting")
            txn.execute(
                "INSERT INTO stats_reporting"
                " (reported_stream_token, reported_time)"
                " VALUES (?, ?)",
                (now_reporting, now,)
            )

            if not last_reported:
                logger.info("Calculating daily messages skipped; no last_reported")
                return None

            # Close enough to correct for our purposes.
            yesterday = (now - 24 * 60 * 60)
            since_yesterday_seconds = yesterday - last_reported[0]["reported_time"]
            any_since_yesterday = math.fabs(since_yesterday_seconds) > 60 * 60
            if any_since_yesterday:
                logger.info(
                    "Calculating daily messages skipped; since_yesterday_seconds: %d" %
                    (since_yesterday_seconds,)
                )
                return None

            txn.execute(
                "SELECT COUNT(*) as messages"
                " FROM events NATURAL JOIN event_json"
                " WHERE json like '%m.room.message%'"
                " AND stream_ordering > ?"
                " AND stream_ordering <= ?",
                (
                    last_reported[0]["reported_stream_token"],
                    now_reporting,
                )
            )
            rows = self.cursor_to_dict(txn)
            if not rows:
                logger.info("Calculating daily messages skipped; messages count missing")
                return None
            return rows[0]["messages"]

        ret = yield self.runInteraction("count_messages", _count_messages)
        defer.returnValue(ret)

    @defer.inlineCallbacks
    def _background_reindex_origin_server_ts(self, progress, batch_size):
        target_min_stream_id = progress["target_min_stream_id_inclusive"]
        max_stream_id = progress["max_stream_id_exclusive"]
        rows_inserted = progress.get("rows_inserted", 0)

        INSERT_CLUMP_SIZE = 1000

        def reindex_search_txn(txn):
            sql = (
                "SELECT stream_ordering, event_id FROM events"
                " WHERE ? <= stream_ordering AND stream_ordering < ?"
                " ORDER BY stream_ordering DESC"
                " LIMIT ?"
            )

            txn.execute(sql, (target_min_stream_id, max_stream_id, batch_size))

            rows = txn.fetchall()
            if not rows:
                return 0

            min_stream_id = rows[-1][0]
            event_ids = [row[1] for row in rows]

            events = self._get_events_txn(txn, event_ids)

            rows = []
            for event in events:
                try:
                    event_id = event.event_id
                    origin_server_ts = event.origin_server_ts
                except (KeyError, AttributeError):
                    # If the event is missing a necessary field then
                    # skip over it.
                    continue

                rows.append((origin_server_ts, event_id))

            sql = (
                "UPDATE events SET origin_server_ts = ? WHERE event_id = ?"
            )

            for index in range(0, len(rows), INSERT_CLUMP_SIZE):
                clump = rows[index:index + INSERT_CLUMP_SIZE]
                txn.executemany(sql, clump)

            progress = {
                "target_min_stream_id_inclusive": target_min_stream_id,
                "max_stream_id_exclusive": min_stream_id,
                "rows_inserted": rows_inserted + len(rows)
            }

            self._background_update_progress_txn(
                txn, self.EVENT_ORIGIN_SERVER_TS_NAME, progress
            )

            return len(rows)

        result = yield self.runInteraction(
            self.EVENT_ORIGIN_SERVER_TS_NAME, reindex_search_txn
        )

        if not result:
            yield self._end_background_update(self.EVENT_ORIGIN_SERVER_TS_NAME)

        defer.returnValue(result)

    def get_current_backfill_token(self):
        """The current minimum token that backfilled events have reached"""
        return -self._backfill_id_gen.get_current_token()

    def get_all_new_events(self, last_backfill_id, last_forward_id,
                           current_backfill_id, current_forward_id, limit):
        """Get all the new events that have arrived at the server either as
        new events or as backfilled events"""
        def get_all_new_events_txn(txn):
            sql = (
                "SELECT e.stream_ordering, ej.internal_metadata, ej.json, eg.state_group"
                " FROM events as e"
                " JOIN event_json as ej"
                " ON e.event_id = ej.event_id AND e.room_id = ej.room_id"
                " LEFT JOIN event_to_state_groups as eg"
                " ON e.event_id = eg.event_id"
                " WHERE ? < e.stream_ordering AND e.stream_ordering <= ?"
                " ORDER BY e.stream_ordering ASC"
                " LIMIT ?"
            )
            if last_forward_id != current_forward_id:
                txn.execute(sql, (last_forward_id, current_forward_id, limit))
                new_forward_events = txn.fetchall()

                if len(new_forward_events) == limit:
                    upper_bound = new_forward_events[-1][0]
                else:
                    upper_bound = current_forward_id

                sql = (
                    "SELECT -event_stream_ordering FROM current_state_resets"
                    " WHERE ? < event_stream_ordering"
                    " AND event_stream_ordering <= ?"
                    " ORDER BY event_stream_ordering ASC"
                )
                txn.execute(sql, (last_forward_id, upper_bound))
                state_resets = txn.fetchall()

                sql = (
                    "SELECT -event_stream_ordering, event_id, state_group"
                    " FROM ex_outlier_stream"
                    " WHERE ? > event_stream_ordering"
                    " AND event_stream_ordering >= ?"
                    " ORDER BY event_stream_ordering DESC"
                )
                txn.execute(sql, (last_forward_id, upper_bound))
                forward_ex_outliers = txn.fetchall()
            else:
                new_forward_events = []
                state_resets = []
                forward_ex_outliers = []

            sql = (
                "SELECT -e.stream_ordering, ej.internal_metadata, ej.json,"
                " eg.state_group"
                " FROM events as e"
                " JOIN event_json as ej"
                " ON e.event_id = ej.event_id AND e.room_id = ej.room_id"
                " LEFT JOIN event_to_state_groups as eg"
                " ON e.event_id = eg.event_id"
                " WHERE ? > e.stream_ordering AND e.stream_ordering >= ?"
                " ORDER BY e.stream_ordering DESC"
                " LIMIT ?"
            )
            if last_backfill_id != current_backfill_id:
                txn.execute(sql, (-last_backfill_id, -current_backfill_id, limit))
                new_backfill_events = txn.fetchall()

                if len(new_backfill_events) == limit:
                    upper_bound = new_backfill_events[-1][0]
                else:
                    upper_bound = current_backfill_id

                sql = (
                    "SELECT -event_stream_ordering, event_id, state_group"
                    " FROM ex_outlier_stream"
                    " WHERE ? > event_stream_ordering"
                    " AND event_stream_ordering >= ?"
                    " ORDER BY event_stream_ordering DESC"
                )
                txn.execute(sql, (-last_backfill_id, -upper_bound))
                backward_ex_outliers = txn.fetchall()
            else:
                new_backfill_events = []
                backward_ex_outliers = []

            return AllNewEventsResult(
                new_forward_events, new_backfill_events,
                forward_ex_outliers, backward_ex_outliers,
                state_resets,
            )
        return self.runInteraction("get_all_new_events", get_all_new_events_txn)


AllNewEventsResult = namedtuple("AllNewEventsResult", [
    "new_forward_events", "new_backfill_events",
    "forward_ex_outliers", "backward_ex_outliers",
    "state_resets"
])
