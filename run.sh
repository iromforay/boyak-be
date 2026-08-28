#!/usr/bin/env bash
cd "$(dirname "$0")"
venv/bin/python -m uvicorn app.main:app --reload --port 88
