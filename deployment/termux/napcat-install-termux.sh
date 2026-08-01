#!/bin/bash

clear

MAGENTA='\033[0;1;35;95m'
RED='\033[0;1;31;91m'
GREEN='\033[0;1;32;92m'
NC='\033[0m'

execute_command() {
    echo -e "${2}中...${NC}"
    $1
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}$2 成功${NC}"
    else
        echo -e "${RED}$2 失败${NC}"
        exit 1
    fi
}

echo -e "准备proot-distro环境中..."
if command -v proot-distro >/dev/null && command -v screen >/dev/null; then
    echo -e "${GREEN}准备proot-distro环境成功${NC}"
else
    apt update -y && apt install -y proot-distro screen
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}准备proot-distro环境成功${NC}"
    else
        echo -e "${RED}准备proot-distro环境失败${NC}"
        exit 1
    fi
fi

if [ -d "$PREFIX/var/lib/proot-distro/containers/napcat/rootfs" ]; then
    echo -e "${GREEN}NapCat容器已存在，继续初始化${NC}"
else
    execute_command "proot-distro install $HOME/debian-trixie-aarch64-pd-v4.29.0.tar.xz --name napcat" "安装napcat容器"
fi
execute_command "proot-distro copy $HOME/napcat-install-linux.sh napcat:/root/napcat.sh" "复制NapCat安装脚本"

echo -e "${GREEN}正在初始化napcat容器...${NC}"
init_cmd="apt update -y && \
apt install -y sudo curl libgcrypt20 && \
sudo bash /root/napcat.sh --docker n --cli n && \
apt clean && \
rm -rf /tmp/* /var/lib/apt/lists"
proot-distro login napcat \
    -e http_proxy=http://127.0.0.1:17892 \
    -e https_proxy=http://127.0.0.1:17892 \
    -e HTTP_PROXY=http://127.0.0.1:17892 \
    -e HTTPS_PROXY=http://127.0.0.1:17892 \
    -- bash -c "$init_cmd"
if [ $? -ne 0 ]; then
    proot-distro remove napcat
    echo -e "${RED}napcat容器初始化失败。${NC}"
    exit 1
fi
echo -e "${RED}napcat容器安装成功, 上方信息无需关注, 请参考下方说明⬇${NC}"

echo -e "\n安装完成, 请输入${GREEN} proot-distro sh napcat -- bash -c \"xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox\" ${NC}命令启动。"
echo -e "保持后台运行 请输入${GREEN} screen -dmS napcat bash -c 'proot-distro sh napcat -- bash -c \"xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox\"'${NC}"
echo -e "后台快速登录 请输入${GREEN} screen -dmS napcat bash -c 'proot-distro sh napcat -- bash -c \"xvfb-run -a /root/Napcat/opt/QQ/qq --no-sandbox -q QQ号码\"'${NC}"
echo -e "进入容器内部 请输入${GREEN} proot-distro login napcat ${NC}"
echo -e "容器数据位置${MAGENTA} /data/data/com.termux/files/usr/var/lib/proot-distro/installed-rootfs/napcat${NC}"
echo -e "Napcat安装位置(容器外真实路径)${MAGENTA} /data/data/com.termux/files/usr/var/lib/proot-distro/installed-rootfs/napcat/root/Napcat/opt/QQ/resources/app/app_launcher/napcat${NC}"
echo -e "注意, 您可以随时使用${GREEN}screen -r napcat${NC}来进入后台进程并使用${GREEN}ctrl + a + d${NC}离开(离开不会关闭后台进程)。"
echo -e "${GREEN}WEB_UI访问密钥请查看 Napcat安装位置/config/webui.json ${NC}"
