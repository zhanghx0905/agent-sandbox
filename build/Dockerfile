# 基于官方 Python 3.11 slim（Debian）
FROM python:3.11-slim

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 设置国内 apt 源（清华大学镜像站）
RUN set -eux; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g; \
                s|http://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list; \
    fi; \
    if ls /etc/apt/sources.list.d/*.list >/dev/null 2>&1; then \
        sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g; \
                s|http://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/*.list; \
    fi; \
    if ls /etc/apt/sources.list.d/*.sources >/dev/null 2>&1; then \
        sed -i 's|http://deb.debian.org/debian|https://mirrors.tuna.tsinghua.edu.cn/debian|g; \
                s|http://security.debian.org/debian-security|https://mirrors.tuna.tsinghua.edu.cn/debian-security|g' /etc/apt/sources.list.d/*.sources; \
    fi
# 安装基础工具 + 编译依赖
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        tzdata \
        ca-certificates \
        build-essential \
        libssl-dev \
        libffi-dev \
        python3-dev \
        libjpeg-dev \
        zlib1g-dev \
        curl \
        wget \
        git \
        && rm -rf /var/lib/apt/lists/*

# 设置 pip 国内源
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple

# 预装常用库（合并为单层安装，减少镜像层）
RUN pip install --no-cache-dir \
    numpy==1.26.0 \
    pandas==2.1.0 \
    scikit-learn==1.3.0 \
    matplotlib==3.7.2 \
    seaborn \
    plotly \
    pillow==10.0.0 \
    imageio \
    requests==2.31.0 \
    jupyterlab \
    ipykernel \
    ipywidgets \
    pandas-profiling \
    openpyxl \
    xlsxwriter \
    xlrd \
    pyxlsb \
    python-docx \
    python-pptx \
    pypdf \
    pdfplumber \
    reportlab \
    markdown2 \
    markdownify \
    python-dotenv \
    python-multipart \
    rapidfuzz \
    dateparser \
    pyarrow \
    pytesseract \
    opencv-python-headless \
    boto3==1.34.0 \
    docker==7.1.0 \
    rich \
    tqdm \
    loguru \
    psutil \
    paramiko

# 清理临时文件
RUN apt-get clean && \
    rm -rf /tmp/* /var/tmp/*

# 声明工作目录（与沙箱脚本约定一致）
VOLUME /work

# 指定入口（可选，用于调试）
# CMD ["python", "-c", "import numpy; print('Sandbox ready.')"]