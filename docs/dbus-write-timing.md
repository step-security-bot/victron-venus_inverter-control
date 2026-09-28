# Attributing slow acknowledged D-Bus writes

The native client records diagnostic phase durations for SetValue calls taking
at least 200 ms. It stores at most 64 records and the existing performance worker
drains them into the normal log approximately every five seconds. The write path
does not log or publish these records. A stalled sink can lose older diagnostic
records when the bounded queue fills; it cannot build an unbounded backlog.

Each native record contains a D-Bus unique sender name, message serial, process and
OS thread IDs, monotonic stage anchors and durations.
Command values, message bodies, destination services and paths are omitted.

- `total_ms`: after Variant/Message construction, immediately before resolving
  the connection, through return to the synchronous caller.
- `setup_ms`: connection resolution and submission preparation.
- `dispatch_ms`: cross-thread submission until the event-loop coroutine starts.
- `await_reply_ms`: awaiting the client call, including client-side sending,
  service response, event-loop scheduling and client reply processing.
- `caller_wakeup_ms`: coroutine completion until the synchronous caller resumes.
- `call_to_reply_observer_ms`: entry to `bus.call()` until the public incoming-message
  observer sees a matching METHOD_RETURN or ERROR. This includes client marshalling,
  sending, service work and event-loop receive scheduling; it is not socket time.
- `reply_observer_to_resume_ms`: that observer until the `bus.call()` coroutine
  resumes (or raises). The observer returns `None`; the normal reply handler still
  supplies the ACK and remains responsible for accepting or rejecting the write.

`anchors` exports the monotonic seconds underlying these phases, including
`call_started_at`, `reply_observed_at` and `call_finished_at`. `pid`,
`caller_native_tid` and `loop_native_tid` identify the Linux /proc process and
threads for a simultaneous, separately bounded scheduler capture. They are not
Python thread identifiers. Missing/late phases remain unavailable. When a
cancellation completes after the caller has returned, anchors may be later than
`returned_at`; derived durations crossing that boundary remain `None`.

`phase: write_lock` is a separate record when the outer VictronDBus `_set_lock`
wait alone reaches 200 ms, before either native or CLI transport. It carries the
caller OS thread ID, `lock_requested_at`, `lock_acquired_at` and `lock_wait_ms`.
At most 64 lock records are retained separately. These are not native-call totals;
matching process/thread and monotonic bounds identifies the following synchronous
native call, when present. Fast lock waits are not emitted. CLI fallback and
Variant/Message construction are still outside the native total. A rejected native
call followed by CLI does not count as two lock acquisitions.

Both kinds retain the existing `Native D-Bus write timing` log prefix; consumers
must distinguish the `phase` field. Records can also be dropped when their
drain lock is busy: optional diagnostics never wait for that lock on the write path.
There is no new logging, tracing, system call sampling or exporter I/O in a reply
observer. The observer uses dbus-fast's public `add_message_handler` and
`remove_message_handler` API and is removed on success, rejection, error or cancellation.

An unavailable phase is `None`, for example when the caller times out before a
queued coroutine starts. A queued request that has expired is still never sent;
the existing cancellation, timeout and acknowledgement semantics are unchanged.
Records are copied before enqueueing, so a late cancellation cannot revise an
already reported observation.

`await_reply_ms` is **not** the wire call-to-reply measurement. Match the record's
`sender` and `serial` against a simultaneous passive `dbus-monitor --profile`
capture of the inverter SetValue path and its method return. The independent
profile supplies an independent observer interval for that exact request, not a
kernel timestamp, pure service execution time or physical actuator latency. Because the
worker logs later, do not correlate by log emission time alone. A serial of
`None` means a wire dispatch was not observed by the client.

The native phases do not include Variant/Message construction, earlier waits
for the watchdog guard or VictronDBus `_set_lock`, CLI fallback, or the rest of
the control cycle. Compare them with separate lock records and existing
`setvalue_ms` and cycle-stage measurements. Monotonic clocks require a fresh
clock-domain mapping when compared with realtime packet timestamps; background
log emission time is not that mapping. Neither CPU counters nor this observer
prove scheduler run-queue or GIL contention. Percentiles from different rolling
windows cannot be subtracted to attribute a single call.

For hardware acceptance, collect a bounded passive window after identifying the
installed source. Preserve current control flags and watchdog limits. Report
deadline and write-error counter deltas alongside paired request traces. This
diagnostic change does not establish a real-time deadline guarantee.
