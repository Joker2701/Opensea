.PHONY: test demo fixtures lint scan bot

test:
	python3 -m unittest discover -s tests -t .

demo:
	python3 -m spreadbot --offline demo

fixtures:
	python3 tools/make_fixtures.py

scan:
	python3 -m spreadbot -c config/config.yaml scan --save

bot:
	python3 -m spreadbot -c config/config.yaml bot
