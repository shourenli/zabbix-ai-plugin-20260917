# IDC 现场恢复清单（apt 操作后服务器异常）

> 适用场景：在生产/物理服务器上执行过 `apt install` / `apt update` 后，
> 出现服务不可用甚至 **SSH 连不上**，需要到 IDC 接显示器+键盘现场处理。
>
> **打印本文带过去**（现场往往不方便上网查文档）。
> 原则：**先观察取证 → 再动手；先只读检查 → 再修改；每一步拍照留存。**

---

## 0. 出发前准备（现在就能做）

- [ ] **打印本文**（或存手机/笔记本离线副本）
- [ ] **U 盘一个（≥4GB），写入 Ubuntu Live ISO**（用 Rufus / Ventoy / balenaEtcher）
      用途：系统起不来时做救援。**Live 版本尽量与服务器一致**（如 24.04），便于 chroot 修复
- [ ] 笔记本 + 网线（现场查资料、记录、必要时直连调试）
- [ ] 手机（**拍照/录像：开机全过程、报错画面、GRUB 菜单** —— 这是最关键的证据）
- [ ] 备用 USB 键盘（现场键盘常不好用）
- [ ] 记录本机关键信息（下方第 5 节，建议先填好）

> 💡 **先问 IDC 有没有 IP-KVM / 远程控制台**（很多机房可临时开通）。
> 有的话就不必跑现场，直接在远程控制台操作。

---

## 1. 到场第一件事：先看，别动

- [ ] 显示器接上后：**屏幕有内容吗？**
  - 有登录提示符 / 图形登录界面 → **系统已启动**，走第 2 节
  - 卡在 GRUB / `emergency mode` / `initramfs` / 黑屏 → 走第 3 节
  - 完全无信号 → 检查电源、显示器线、机器是否在运行（风扇/指示灯）
- [ ] 按 `Ctrl+Alt+F2` 试试能否切到文本终端
- [ ] **先拍照**，再操作键盘

> ⚠️ 如果屏幕是正常的登录界面 —— 说明**机器根本没重启**，
> 那就只是服务/网络问题，**绝对不要重启**，直接在第 2 节里就地修。

---

## 2. 系统起来了：按顺序查（先只读）

登录后依次执行（命令都可复制）：

```bash
# 1) 是否刚重启过？uptime 很小 = 重启过
uptime; last reboot | head -5

# 2) ★ 磁盘与 inode（apt 崩溃第一大原因）
df -h; df -i

# 3) dpkg 是否中断（有输出 = 没配完）
dpkg --audit
dpkg -l | awk '$1 !~ /^ii/ {print}' | head -20

# 4) 哪些服务挂了
systemctl --failed

# 5) 关键服务状态
systemctl status ssh mysql nginx zabbix-server zabbix-agent --no-pager -l
systemctl status 'php*-fpm' --no-pager -l

# 6) 本次启动的错误日志
journalctl -b -p err --no-pager | tail -50

# 7) 这次 apt 到底装/升了什么
grep -E "^(Start-Date|Commandline|Install|Upgrade|Remove)" /var/log/apt/history.log | tail -30
```

### 判读表

| 现象 | 结论 | 跳转 |
|---|---|---|
| `df -h` 或 `df -i` 显示 **100%** | 磁盘/inode 满（最可能） | 2.1 |
| `dpkg --audit` 有输出 | dpkg 中断 | 2.2 |
| 服务报 `No space left on device` | 磁盘满导致服务起不来 | 2.1 → 2.2 |
| `ssh` 为 `inactive`/`failed`，其他正常 | 只是 sshd | 2.3 |
| 一切正常、`systemctl --failed` 为空 | 可能是网络/硬件层 | 2.4 |

### 2.1 磁盘满（最常见）

```bash
# 看谁占地方
du -xh --max-depth=1 /var | sort -h | tail -10
du -xh --max-depth=1 /    | sort -h | tail -10

# 安全清理（按需，逐条确认后执行）
journalctl --vacuum-size=200M          # 清理 systemd 日志
apt-get clean                          # 清理 apt 缓存
ls -la /var/cache/apt/archives/*.deb | head

# 若 /boot 满：查看旧内核，删最老的（务必先 ls 看清楚再删！）
ls -la /boot
dpkg -l 'linux-image-*' | grep ^ii
```

**若 `/boot` 满且内核/initramfs 不完整**，清理后必须补：
```bash
update-initramfs -u -k all
update-grub
```

### 2.2 dpkg 中断

```bash
dpkg --configure -a
apt-get -f install
dpkg --audit          # 应无输出
```

### 2.3 只是 sshd 起不来

```bash
systemctl status ssh --no-pager -l
systemctl restart ssh
ss -tlnp | grep :22                    # 应看到 LISTEN
journalctl -u ssh -n 50 --no-pager
# 本机自测
ssh -o StrictHostKeyChecking=no localhost true && echo "sshd OK"
```

### 2.4 服务都不正常？检查防火墙与网络

```bash
ufw status                             # 或 iptables -L -n | head -30
ip a; ip r                             # IP/网关是否正常
ethtool eth0 2>/dev/null | grep -E "Link detected|Speed"
ping -c2 网关
```
同时检查**物理层**：网线是否松动、交换机端口灯是否亮、端口是否被误关。

### 2.5 按序把服务拉起来

```bash
systemctl restart mysql                # 先数据库
systemctl restart 'php*-fpm'           # 再 PHP（Zabbix 前端依赖）
systemctl restart nginx
systemctl restart zabbix-server zabbix-agent
systemctl restart ssh
systemctl --failed                     # 应清空
```

---

## 3. 系统起不来：U 盘 Live 救援

### 3.1 进 Live 环境
1. 插 U 盘，开机按 `F2/F10/F11/F12/Del`（看开机提示）进 BIOS/启动菜单，选 USB 启动
2. 选择 **Try Ubuntu**（**不要** Install）

### 3.2 找到系统分区并只读检查
```bash
lsblk -f                 # 认准 / 所在分区（多为最大 ext4）；记录 /boot 是否独立分区
sudo mount /dev/sdaX /mnt            # ← 替换 sdaX
df -h /mnt                           # 先看空间占用
sudo du -xh --max-depth=2 /mnt/var | sort -h | tail -15
```

### 3.3 清理空间（若 / 或 /boot 满）
```bash
sudo rm -rf /mnt/var/cache/apt/archives/*.deb
sudo journalctl --directory=/mnt/var/log/journal --vacuum-size=200M 2>/dev/null \
  || sudo rm -rf /mnt/var/log/journal/*
# /boot 独立分区时，先挂它再看
# sudo mount /dev/sdaY /mnt/boot && ls -la /mnt/boot
```

### 3.4 chroot 进去修 dpkg / initramfs
```bash
for d in dev proc sys run; do sudo mount --bind /$d /mnt/$d; done
sudo chroot /mnt /bin/bash

dpkg --configure -a
apt-get -f install
update-initramfs -u -k all        # /boot 曾满、initramfs 不完整时必须
update-grub
exit
```

### 3.5 卸载并重启
```bash
for d in dev proc sys run; do sudo umount /mnt/$d; done
sudo umount /mnt
sudo reboot
```

> ⚠️ 全程**先只读检查、确认后再动手**；每一步拍照。
> 如果机器是云主机或挂载了 RAID/LVM，先 `lsblk`、`vgscan`、`lvs` 看清楚，不要猜设备名。

---

## 4. 恢复后验证

```bash
systemctl --failed                                             # 应为空
systemctl is-active ssh mysql nginx zabbix-server zabbix-agent
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:801/  # Zabbix 前端应 200
df -h
```

再到 Zabbix 前端确认 **Latest data 没有长时间空档**（监控是否断过）。

---

## 5. 现场必带信息（出发前填好）

| 项 | 值 |
|---|---|
| 服务器 IP / 用途 | |
| 操作系统版本 | |
| Zabbix 前端端口 | 例：801 |
| 本插件部署路径 | /opt/zabbix-ai-plugin |
| 插件数据库 | zabbix_ai_plugin / 账号 zabbix_ai |
| 是否有备份/快照 | |
| 联系人 / 电话 | |

---

## 6. 回来之后要做的（避免再发生）

1. 把**真实原因**和修复过程记入运维记录
2. 生产机**禁止** `apt upgrade` 与无人值守自动升级
3. 用 `apt-mark hold` 钉住关键包：
   ```bash
   apt-mark hold zabbix-server-mysql zabbix-frontend-php zabbix-agent mysql-server nginx
   ```
4. 本插件的依赖安装**改用 `uv`，完全不碰 apt**（见 `DEPLOYMENT.md` 3.1 方案 A）
5. 给关键服务器配置**带外管理**（IP-KVM）或至少有人在机房的值班联系
6. 重要操作前先打快照/备份 —— 特别是任何 `apt` 相关操作
