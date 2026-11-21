# --- プロジェクトが足す make の命令（第4〜5層の余白）（LAYERS.md「第4〜5層の『余白』」）---
.PHONY: ezr-test ezr-test-launcher ezr-build-amd64 ezr-test-amd64

EZR_NAME  := $(shell sed -n 's/^name: //p' docker-compose.yml)
EZR_AMD64 := $(EZR_NAME):dev-amd64
EZR_RUN   := docker run --rm --user rstudio -e HOME=/home/rstudio -v "$(CURDIR):/app" -w /app
EZR_TEST  := Rscript -e "testthat::test_dir('tests/testthat', filter = 'project_ezr', reporter = 'summary', stop_on_failure = TRUE)"

ezr-test:  ## make build したイメージで、EZR の起動の試験だけを使い捨てのコンテナで走らせる
	docker compose run --rm --user rstudio task $(EZR_TEST)

ezr-build-amd64:  ## Windows と同じ amd64 のイメージを作る（この Mac ではエミュレーションで、時間がかかる）
	docker build --platform linux/amd64 --target dev -t $(EZR_AMD64) .

ezr-test-amd64:  ## ezr-build-amd64 したイメージで、EZR の起動の試験を走らせる
	$(EZR_RUN) --platform linux/amd64 $(EZR_AMD64) $(EZR_TEST)

ezr-test-launcher:  ## standalone の起動の流れ（launcher/standalone.py）の試験を、ホストで走らせる（VM は起こさない）
	PYTHONDONTWRITEBYTECODE=1 python3 -I -B ../tests/test_standalone.py
