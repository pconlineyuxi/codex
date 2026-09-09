"""Run: python -m bi_check_agent.worker [--once] [--db PATH]."""
import argparse
import logging
import signal
import threading
from .monitor import MonitorStore


def tick(store, evaluate=None, stop_event=None):
    if evaluate is None:
        from .service import evaluate_plan
        evaluate = evaluate_plan
    results = []
    for plan, start, end in store.due_windows():
        if stop_event is not None and stop_event.is_set():
            break
        try:
            result = store.run(plan, start, end, evaluate)
            results.append(result)
            logging.info('plan=%s window=%s/%s status=%s', plan['id'], start, end, result['status'])
        except Exception as exc:
            logging.error('Plan execution failed: %s', type(exc).__name__)
    store.dispatch()
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='Product Profit daily New York monitoring worker')
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--db', default=None)
    parser.add_argument('--interval', type=int, default=30)
    args = parser.parse_args(argv)
    if args.interval < 1:
        parser.error('--interval must be positive')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    from dotenv import load_dotenv
    load_dotenv()
    store = MonitorStore(args.db)
    while not stopped.is_set():
        try:
            tick(store, stop_event=stopped)
        except Exception as exc:
            logging.error('Worker cycle failed: %s', type(exc).__name__)
            if args.once:
                return 1
        if args.once:
            return 0
        stopped.wait(args.interval)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
