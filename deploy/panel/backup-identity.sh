#!/bin/sh
# 把 identity/ 打一份快照到 /var/backups/delivery/，保留固定份数。
#
# 为什么需要它：identity/ 里放着**重建不出来**的东西 —— tickets.json（每张申请单的
# 完整事件流、飞书审批实例号、凭证记录）、people.json 名册、manual-links.json 人工
# 对应关系、admins.json、request-templates.json、approval.json。assets.json /
# policies.json / inventory.json 重跑采集能回来，前面那些丢了就是丢了。
# 这台机上原本一个备份任务都没有，只有出事后临时 cp 的几个 .bak 文件。
#
# 它挡什么：误覆盖（rsync 少打一个路径）、写坏（磁盘满时截断）、手滑删除。
# 它**不挡**整机丢失 —— 备份和原文件在同一块盘上。异地副本是另一件事。
set -eu

SRC=${DELIVERY_IDENTITY_DIR:-/opt/infra/identity}
DST=${DELIVERY_BACKUP_DIR:-/var/backups/delivery}
KEEP=${DELIVERY_BACKUP_KEEP:-48}

[ -d "$SRC" ] || { echo "没有 $SRC，不备份" >&2; exit 1; }

# 0700：备份里有申请人姓名、邮箱、手机号，和原文件同样只给属主看
mkdir -p "$DST"
chmod 700 "$DST"

stamp=$(date +%Y%m%d-%H%M%S)
tmp="$DST/.identity-$stamp.tar.gz.part"
out="$DST/identity-$stamp.tar.gz"

# **先写临时文件再改名**：tar 跑到一半断电/磁盘满时，留下的是 .part 而不是一个
# 看起来正常、解开却缺文件的 .tar.gz —— 后者会在真要恢复的那天才被发现
# 排除锁文件和自己的临时产物；.bak-* 是人工留下的，照旧收进来
umask 077
tar -czf "$tmp" -C "$(dirname "$SRC")" \
    --exclude='*.lock' --exclude='.*.tmp' \
    "$(basename "$SRC")"
mv "$tmp" "$out"

# 只删自己这个命名格式的，且**按文件名排序**（时间戳是定长的，字典序即时间序）——
# 用 ls -t 的话，恢复演练时 touch 过的旧备份会被当成最新的留下来
count=$(find "$DST" -maxdepth 1 -name 'identity-*.tar.gz' | wc -l)
if [ "$count" -gt "$KEEP" ]; then
    find "$DST" -maxdepth 1 -name 'identity-*.tar.gz' | sort | head -n "$((count - KEEP))" |
        while IFS= read -r old; do rm -f "$old"; done
fi

echo "已备份 $out（$(du -h "$out" | cut -f1)），现存 $(find "$DST" -maxdepth 1 -name 'identity-*.tar.gz' | wc -l) 份"
