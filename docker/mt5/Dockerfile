# MetaTrader 5 terminal + Windows Python (MetaTrader5 package) running headless under Wine.
# Nothing is displayed: the GUI renders into a virtual X server (Xvfb) inside the container.
# The only thing exposed is an RPyC port on a private Docker network that only the bridge can reach.
FROM debian:bookworm-slim

ARG PYTHON_WIN_VERSION=3.11.9
ARG MT5_SETUP_URL=https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe

ENV DEBIAN_FRONTEND=noninteractive \
    WINEDEBUG=-all \
    WINEARCH=win64 \
    WINEPREFIX=/home/trader/.wine \
    WINEDLLOVERRIDES="mscoree,mshtml=" \
    DISPLAY=:99 \
    HOME=/home/trader

RUN dpkg --add-architecture i386 \
 && apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && mkdir -pm755 /etc/apt/keyrings \
 && curl -fsSL https://dl.winehq.org/wine-builds/winehq.key | gpg --dearmor -o /etc/apt/keyrings/winehq-archive.key \
 && curl -fsSL -o /etc/apt/sources.list.d/winehq-bookworm.sources https://dl.winehq.org/wine-builds/debian/dists/bookworm/winehq-bookworm.sources \
 && apt-get update \
 && apt-get install -y --install-recommends winehq-stable \
 && apt-get install -y --no-install-recommends xvfb xauth unzip procps tini \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -m -u 1000 trader

USER trader
WORKDIR /home/trader

# Windows Python (official *embeddable* build - no installer needed under Wine) + the official
# MetaTrader5 package + RPyC server.
RUN mkdir -p "$WINEPREFIX/drive_c" \
 && xvfb-run -a wineboot --init && wineserver -w \
 && curl -fsSL -o /tmp/py.zip "https://www.python.org/ftp/python/${PYTHON_WIN_VERSION}/python-${PYTHON_WIN_VERSION}-embed-amd64.zip" \
 && mkdir -p "$WINEPREFIX/drive_c/Python" && unzip -q /tmp/py.zip -d "$WINEPREFIX/drive_c/Python" && rm /tmp/py.zip \
 && sed -i 's/^#import site/import site/' "$WINEPREFIX"/drive_c/Python/python3*._pth \
 && curl -fsSL -o "$WINEPREFIX/drive_c/Python/get-pip.py" https://bootstrap.pypa.io/get-pip.py \
 && wine 'C:\Python\python.exe' 'C:\Python\get-pip.py' --no-warn-script-location \
 && wine 'C:\Python\python.exe' -m pip install --no-cache-dir --disable-pip-version-check \
      MetaTrader5 rpyc==6.0.2 numpy \
 && wine 'C:\Python\python.exe' -c "import MetaTrader5, rpyc; print('MetaTrader5', MetaTrader5.__version__)" \
 && wineserver -w

# MetaTrader 5 terminal (official MetaQuotes web installer). The installer launches the
# terminal when it finishes, so it is time-boxed and Wine is shut down afterwards.
RUN curl -fsSL -o /tmp/mt5setup.exe "${MT5_SETUP_URL}" \
 && (timeout 900 xvfb-run -a wine /tmp/mt5setup.exe /auto || true) \
 && (wineserver -k || true) && rm -f /tmp/mt5setup.exe \
 && test -f "$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"

COPY --chown=trader:trader entrypoint.sh rpyc_server.py /home/trader/
EXPOSE 18812
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD bash -c 'echo > /dev/tcp/127.0.0.1/18812' || exit 1
ENTRYPOINT ["/usr/bin/tini", "--", "/bin/bash", "/home/trader/entrypoint.sh"]
