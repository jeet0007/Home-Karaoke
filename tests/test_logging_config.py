import json
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import logging_config  # noqa: E402


class LoggingConfigTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.log_dir = tmp.name
        self.addCleanup(self._reset_logging_config)

    def _reset_logging_config(self):
        # configure() is idempotent by design (a global flag), so tests must
        # reset it themselves between cases - otherwise the second test to
        # call configure() would silently no-op against the first test's
        # (already-cleaned-up) tempdir.
        logging_config._configured = False
        logger = logging.getLogger(logging_config.LOGGER_NAME)
        for handler in list(logger.handlers):
            if not isinstance(handler, logging.NullHandler):
                logger.removeHandler(handler)
                handler.close()
        logger.propagate = True

    def test_configure_creates_log_dir_and_file(self):
        log_dir = os.path.join(self.log_dir, "nested", "logs")
        self.assertFalse(os.path.isdir(log_dir))
        logging_config.configure(log_dir=log_dir)
        self.assertTrue(os.path.isdir(log_dir))
        self.assertTrue(os.path.isfile(os.path.join(log_dir, "pipeline.log")))

    def test_emitted_record_is_one_json_line_with_expected_fields(self):
        logging_config.configure(log_dir=self.log_dir)
        logger = logging.getLogger("karaoke.pipeline")
        logger.info("stage lyrics ok", extra={"song_id": 1, "run_id": "abc", "stage": "lyrics"})

        log_path = os.path.join(self.log_dir, "pipeline.log")
        with open(log_path, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["message"], "stage lyrics ok")
        self.assertEqual(record["song_id"], 1)
        self.assertEqual(record["run_id"], "abc")
        self.assertEqual(record["stage"], "lyrics")
        self.assertIn("level", record)
        self.assertIn("time", record)

    def test_double_configure_does_not_duplicate_handlers(self):
        logging_config.configure(log_dir=self.log_dir)
        logging_config.configure(log_dir=self.log_dir)
        logger = logging.getLogger(logging_config.LOGGER_NAME)
        real_handlers = [h for h in logger.handlers if not isinstance(h, logging.NullHandler)]
        self.assertEqual(len(real_handlers), 1)

    def test_configure_defaults_to_karaoke_log_dir_env_var(self):
        env_dir = os.path.join(self.log_dir, "from-env")
        old = os.environ.get("KARAOKE_LOG_DIR")
        os.environ["KARAOKE_LOG_DIR"] = env_dir
        try:
            logging_config.configure()
        finally:
            if old is None:
                os.environ.pop("KARAOKE_LOG_DIR", None)
            else:
                os.environ["KARAOKE_LOG_DIR"] = old
        self.assertTrue(os.path.isfile(os.path.join(env_dir, "pipeline.log")))


if __name__ == "__main__":
    unittest.main()
