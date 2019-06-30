#!/bin/bash
pkill -f -INT reactorconsole.py
pkill -f -INT reactorconsole.py
pkill -f -INT reactorconsole.py
pkill -f -INT reactorconsole.py

source /home/hacklab/.virtualenvs/reactorconsole/bin/activate
cd ~/devel/odysseus-python/reactorconsole
./reactorconsole.py --id jump_reactor --url http://192.168.1.2
