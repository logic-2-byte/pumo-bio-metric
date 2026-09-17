#!/bin/bash
# Must stay one worker — see the comment at the top of gunicorn_config.py for why.
gunicorn -c gunicorn_config.py app.main:app