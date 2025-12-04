define gen_model_cmd
	poetry run datamodel-codegen \
		--input $(1) \
		--output $(2) \
		--input-file-type openapi \
		--strict-nullable \
		--use-default \
		--use-type-alias \
		--set-default-enum-member \
		--use-one-literal-as-default \
		--use-standard-collections \
		--use-union-operator \
		--enum-field-as-literal one \
		--force-optional \
		--use-specialized-enum \
		--collapse-root-models \
		--use-annotated \
		--target-python-version 3.13 \
		--output-model-type pydantic_v2.BaseModel
endef

all: generate_models

lint:
	poetry run ruff check --fix tidal_bot
	poetry run mypy tidal_bot

format:
	poetry run ruff format tidal_bot

generate_models: tidal_bot/spotify/model.py

%.py: %.yaml
	$(call gen_model_cmd,$<,$@)
	poetry run ruff format $@

tidal_bot/spotify/model.yaml:
	wget https://raw.githubusercontent.com/sonallux/spotify-web-api/refs/heads/main/fixed-spotify-open-api.yml -O $@
	sed -rzi 's/( +discriminator:\n)( +propertyName: type)/# \1# \2/g' $@
