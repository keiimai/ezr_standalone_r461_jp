# --- 研究プロジェクトが足すビルドの手順（第4〜5層の余白。root の sh -eu で動き、失敗するとビルドが止まる）（DOCKER.md 2 節）---
set -eu
page=/usr/lib/rstudio-server/www/index.htm
sed -i -e '0,/<html lang="en">/s//<html lang="en" translate="no">/' \
       -e '0,/<head>/s//<head>\n  <meta name="google" content="notranslate" \/>/' "$page"
grep -q '<html lang="en" translate="no">' "$page"
grep -q '<meta name="google" content="notranslate" />' "$page"
