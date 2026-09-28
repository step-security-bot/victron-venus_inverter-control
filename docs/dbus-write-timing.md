# Attributing slow acknowledged D-Bus writes

The native client records diagnostic phase durations for SetValue calls taking
at least 200 ms. It stores at most 64 records and the existing performance worker
drains them into the normal log approximately every five seconds. The write path
does not log or publish these records. A stalled sink can lose older diagnostic
records when the bounded queue fills; it cannot build an unbounded backlog.

Each native record contains a D-Bus unique sender name, message serial, process and
OS thread IDs, monotonic stage anchors and durations.
Command values, message bodies, destination services and paths are omitted.

The dedicated writer additionally observes the public `MessageBus.send()` return
Future. The telemetry reader retains the original bus class. An opt-in subclass
calls the original `send()` exactly once and returns that identical Future;
`call()`, reply handling, timeout and fallback behavior remain unchanged. It does
not await or cancel the send Future, or consume its result/exception.

Three additional monotonic anchors distinguish the send stage:

- `send_started_at`: entry to the original public `send()` for this exact message.
- `send_returned_at`: the original `send()` returned its Future. In dbus-fast
  2.21.1, marshalling and an immediate socket write can happen synchronously here.
- `send_done_observed_at`: the returned Future was observed done, either inline
  when already done or in an event-loop callback. A queued callback can be delayed
  by scheduling; this is not the exact socket-write completion or kernel timestamp.

`send_sync_ms` spans send entry through return. `send_return_to_done_observer_ms`
spans return through the done observation. `send_future_cancelled` is a boolean
when completion was observed, otherwise `None`. Done does not imply success:
the observer deliberately does not retrieve an exception. A disconnect may also
resolve the send Future without delivering the message. The normal confirmed
application reply remains the only write-acceptance decision. Interpret these
anchors as send-stage evidence only for confirmed calls, and retain independent
monitor timing and the existing reply observer.

Observations are keyed by message identity, excluding unrelated bus traffic.
Their callbacks and registrations are removed when the call finishes or is
cancelled, and when the observed connection fails, is replaced or is closed.
Connection cleanup disables new observations on that bus and runs on its original
loop before client shutdown stops it. This only releases diagnostic observers;
it does not change the existing pending transport-task teardown.
Missing or later observations remain `None`; cleanup never cancels the
transport Future or waits for it. Existing strict offline parsers must explicitly
accept these additional anchors/fields before using the new records. Historical
parser schemas and captures must remain unchanged.

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
At most 64 lock records are retained separately. These are not native-call totals.
Process/thread and monotonic bounds locate the contention in a simultaneous
capture, but there is no explicit lock-record to native-serial link. Do not assume
that the next slow native record belongs to the same write: intervening fast
native calls or CLI writes may be unrecorded. Fast lock waits are not emitted. CLI fallback and
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
