"""Source-secret-only worker for the shared database job queue."""

import logging
import signal
import threading

from sqlalchemy.exc import OperationalError

from .acquisition import cleanup_abandoned_acquisitions, cleanup_expired_uploads, process_once
from .db import SessionLocal
from .models import uid
from .worker import heartbeat


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop, owner = threading.Event(), uid()
    active: dict = {}
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    pulse = threading.Thread(target=heartbeat, args=(owner, stop, active, "ACQUISITION"), daemon=True)
    pulse.start()
    logger = logging.getLogger(__name__)
    logger.info("Worker de adquisición listo. lane=ACQUISITION")
    cleanup_ticks = 0
    while not stop.is_set():
        try:
            if not process_once(owner, active):
                cleanup_ticks += 1
                if cleanup_ticks % 60 == 0:
                    with SessionLocal() as db:
                        cleanup_expired_uploads(db)
                        cleanup_abandoned_acquisitions(db)
                        db.commit()
                stop.wait(1)
        except OperationalError:
            logger.info("Esperando metadata de adquisición...")
            stop.wait(3)
    pulse.join(timeout=2)


if __name__ == "__main__":
    main()
