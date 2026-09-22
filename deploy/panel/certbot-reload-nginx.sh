#!/bin/sh
# 证书续期后让 nginx 读新证书。不 reload 的话续期成功了、nginx 还在用旧证书，90 天后照样过期
nginx -t && systemctl reload nginx
