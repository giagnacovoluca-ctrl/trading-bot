#!/bin/bash
source venv/bin/activate
./venv/bin/python -u watchdog.py > watchdog.log 2>&1
