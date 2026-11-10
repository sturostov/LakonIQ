"""Тесты не трогают настоящие данные: своя временная папка вместо ~/Library/Application Support/LakonIQ."""
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="lakoniq-tests-")
os.environ.setdefault("INTERVIEW_DATA", _tmp)
os.environ.setdefault("INTERVIEW_DB", os.path.join(_tmp, "interview.db"))
