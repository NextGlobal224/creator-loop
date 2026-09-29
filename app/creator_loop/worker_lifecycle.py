"""Safe lifecycle decisions for Creator Loop owned worker processes.

Never terminate a process from PID alone. The live process identity must
match the persisted ownership record exactly before process control occurs.
"""

from collections.abc import Callable

from .worker_process import ProcessIdentity, WorkerOwnership, owns_process

ProcessProbe = Callable[[int], ProcessIdentity | None]
ProcessTerminator = Callable[[int], bool]


def stop_owned_worker(
    record: WorkerOwnership,
    probe: ProcessProbe,
    terminate: ProcessTerminator,
) -> bool:
    """Stop only a worker whose live identity exactly matches our record.

    Any probe failure, missing process, or identity mismatch fails closed.
    """
    try:
        actual = probe(record.pid)
    except Exception:
        return False

    if actual is None:
        return False

    if not owns_process(record, actual):
        return False

    try:
        return bool(terminate(record.pid))
    except Exception:
        return False
