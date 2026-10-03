test:
	PYTHONPATH=. python -m pytest -q

run:
	PYTHONPATH=. python -m psdline --config config/psdline.json serve --host 127.0.0.1 --port 8080

demo:
	PYTHONPATH=. python -m psdline --config config/psdline.json demo --fast

maintain-serve:
	PYTHONPATH=. python -m psdline.maintain serve --db var/state/maintain.db --host 127.0.0.1 --port 8090

maintain-demo:
	PYTHONPATH=. python -m psdline.maintain demo

maintain-scan:
	PYTHONPATH=. python -m psdline.maintain scan --db var/state/maintain.db

maintain-verify:
	PYTHONPATH=. python -m psdline.maintain verify --db var/state/maintain.db

deadcode:
	PYTHONPATH=. python tools/deadcode.py psdline
