#!/bin/bash
set -e

INSTALLED=0
PIP_MIRRORS=(
  "https://pypi.tuna.tsinghua.edu.cn/simple"
  "https://mirrors.aliyun.com/pypi/simple/"
  "https://mirrors.ustc.edu.cn/pypi/web/simple"
  "https://mirrors.cloud.tencent.com/pypi/simple"
  "https://pypi.douban.com/simple/"
  "https://repo.huaweicloud.com/repository/pypi/simple"
)

echo ">>> 测速 pip 国内镜像..."
rm -f /tmp/pip_ranked.txt
for u in "${PIP_MIRRORS[@]}"; do
  T=$(curl -s -o /dev/null -w '%{time_total}' --max-time 5 "$u" 2>/dev/null || echo "999")
  echo "  $u : ${T}s"
  echo "${T}|${u}" >> /tmp/pip_ranked.txt
done

sort -n /tmp/pip_ranked.txt > /tmp/pip_sorted.txt
echo ">>> 按速度排序:"
cat /tmp/pip_sorted.txt

while IFS='|' read -r TIME URL; do
  [ -z "$URL" ] && continue
  [ "$INSTALLED" = "1" ] && break
  HOST=$(echo "$URL" | sed 's|.*//||;s|/.*||')
  echo ">>> 尝试: $URL (${TIME}s)"
  pip config set global.index-url "$URL" 2>/dev/null
  pip config set global.trusted-host "$HOST" 2>/dev/null
  if pip install --no-cache-dir --timeout=120 -r requirements.txt 2>&1; then
    INSTALLED=1
    echo ">>> 安装成功! 使用源: $URL"
  else
    echo ">>> 失败, 换下一个源重试..."
  fi
done < /tmp/pip_sorted.txt

if [ "$INSTALLED" = "0" ]; then
  echo ">>> 所有国内镜像均失败, 尝试官方源..."
  pip config unset global.index-url 2>/dev/null || true
  pip config unset global.trusted-host 2>/dev/null || true
  pip install --no-cache-dir --timeout=120 -r requirements.txt
fi

rm -f /tmp/pip_ranked.txt /tmp/pip_sorted.txt
echo ">>> 完成"