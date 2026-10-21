"""Kamil V1 relay: dışarı bağlanan Host ve Client'ı eşleştirip ham baytları aktarır.

Yalnızca standart kütüphane, Python 3.8+ (VPS'te çalışır).
    python relay.py --port 9000 [--host 0.0.0.0] [--token T]

İlk satır (en çok 128 bayt, 5 sn içinde, sonu "\\n"):
    Host:   "H <id> <token>"  ->  "OK"; beklerken "K" kalp atışları, eşleşince "GO"
    Client: "C <id> <token>"  ->  "OK"
    Hata:   "ERR format|auth|offline|full" ve bağlantı kapanır
Sonrası ham bayt akışıdır; içerik uçtan uca şifreli, relay görmez.
"""
import argparse
import asyncio
import hmac
import logging
import re
import secrets
import socket
import sys

PREFACE_TIMEOUT = 5.0   # ilk satırı bu sürede bitirmeyen bağlantı atılır
HEARTBEAT = 20.0        # bekleyen Host'lara K aralığı: NAT'ı canlı tutar, ölü kaydı ayıklar
MAX_PREFACE = 128
MAX_WAITING = 1000
ID_RE = re.compile(rb"[0-9a-z.:_-]{1,64}")

log = logging.getLogger("relay")


def resolve_token(token):
    # Token zorunlu: verilmezse rastgele üretilir, relay yanlışlıkla açık kalmaz
    return token or secrets.token_urlsafe(16)


async def read_line(reader):
    # Bayt bayt okunur: satırdan sonra gelen ham veri okuyucuda bozulmadan kalır
    line = bytearray()
    while len(line) <= MAX_PREFACE:
        ch = await reader.readexactly(1)
        if ch == b"\n":
            return bytes(line)
        line += ch
    raise ValueError("ilk satır çok uzun")


def tune(writer):
    # En iyi çaba. NODELAY: asyncio Windows'ta kabul edilen sokette kendisi açmıyor (proto=0),
    # küçük girdi olayları Nagle + gecikmeli ACK'e takılır. Keepalive: sessizce ölen eşin
    # soketi ~1 dk içinde düşsün (Windows varsayılanı 2 saat).
    sock = writer.get_extra_info("socket")
    if sock is None:
        return
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for name, value in (("TCP_KEEPIDLE", 30), ("TCP_KEEPINTVL", 10), ("TCP_KEEPCNT", 3)):
            if hasattr(socket, name):
                sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), value)
    except OSError:
        pass


def reject(writer, why):
    writer.write(b"ERR " + why + b"\n")


def dead(reader, writer):
    # FIN gelmiş ya da yazma hatasıyla kapanmış kayıt
    return reader.at_eof() or writer.is_closing()


async def pipe(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                return
            writer.write(data)
            await writer.drain()    # yavaş taraf okumayı da yavaşlatır, bellek şişmez
    except OSError:
        pass


class Relay:
    def __init__(self, token):
        if not token:
            raise ValueError("token boş olamaz")
        # shortcut: tek ortak token, kimliğe özel sır yok (token sahipleri güvenilir), upgrade: kimlik başına sır
        self.token = token.encode("utf-8")
        self.waiting = {}    # id -> (reader, writer); yalnızca olay döngüsü iş parçacığı dokunur
        self.tasks = set()   # canlı bağlantı görevleri; stop() bunları sonlandırır
        self.server = None
        self.beat = None

    async def start(self, host, port):
        # shortcut: düz TCP, id+token açık yolda gider (içerik zaten uçtan uca şifreli), upgrade: TLS
        self.server = await asyncio.start_server(self.handle, host, port)
        self.beat = asyncio.ensure_future(self.heartbeat())
        return self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        for _, writer in self.waiting.values():
            writer.close()
        self.waiting.clear()
        pending = [self.beat] + list(self.tasks)
        for task in pending:
            task.cancel()
        await asyncio.wait(pending)

    async def handle(self, reader, writer):
        # shortcut: doğrulama öncesi bağlantı sayısı sınırsız (yalnızca 5 sn zaman aşımı), upgrade: eşzamanlı sınır
        task = asyncio.current_task()
        self.tasks.add(task)
        keep = False
        try:
            tune(writer)
            keep = await self.serve(reader, writer)
        except Exception:   # tek bağlantının hatası relay'i düşürmesin
            log.exception("beklenmeyen hata")
        finally:
            self.tasks.discard(task)
            if not keep:
                writer.close()

    async def serve(self, reader, writer):
        """True dönerse bağlantı bekleyen Host olarak açık kalır."""
        peer = writer.get_extra_info("peername")
        try:
            line = await asyncio.wait_for(read_line(reader), PREFACE_TIMEOUT)
        except ValueError:
            log.warning("ilk satır çok uzun: %s", peer)
            reject(writer, b"format")
            return False
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, OSError):
            log.info("ilk satır gelmedi: %s", peer)
            return False
        parts = line.split(b" ", 2)
        if len(parts) != 3 or parts[0] not in (b"H", b"C") or not ID_RE.fullmatch(parts[1]):
            log.warning("bozuk ilk satır: %s", peer)
            reject(writer, b"format")
            return False
        kind, hid, token = parts
        # Doğrulama, kayda dokunan her şeyden önce: yanlış tokenlı C bekleyen Host'u düşüremez
        if not hmac.compare_digest(token, self.token):
            log.warning("yanlış token: %s", peer)
            reject(writer, b"auth")
            return False
        hid = hid.decode("ascii")
        if kind == b"H":
            return self.register(hid, reader, writer)
        await self.pair(hid, reader, writer)
        return False

    def register(self, hid, reader, writer):
        if hid not in self.waiting and len(self.waiting) >= MAX_WAITING:
            log.warning("kapasite dolu: %s", hid)
            reject(writer, b"full")
            return False
        old = self.waiting.get(hid)
        self.waiting[hid] = (reader, writer)
        if old:   # yeniden başlayan Host kendini onarır: eski kayıt kapatılır
            old[1].close()
        log.info("host %s: %s", "değişti" if old else "kayıtlı", hid)
        writer.write(b"OK\n")
        return True

    async def pair(self, hid, creader, cwriter):
        host = self.waiting.pop(hid, None)
        if host is not None and dead(*host):
            host[1].close()
            host = None
        if host is not None:
            hreader, hwriter = host
            try:
                hwriter.write(b"GO\n")
                await hwriter.drain()
            except OSError:
                hwriter.close()
                host = None
        if host is None:
            log.info("host yok: %s", hid)
            reject(cwriter, b"offline")
            return
        cwriter.write(b"OK\n")
        log.info("eşleşti: %s", hid)
        pipes = [asyncio.ensure_future(pipe(creader, hwriter)),
                 asyncio.ensure_future(pipe(hreader, cwriter))]
        try:
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
        finally:   # bir yön bitince (ya da relay durunca) ikisi de kapanır
            for p in pipes:
                p.cancel()
            hwriter.close()
            cwriter.close()
            await asyncio.wait(pipes)
            log.info("bitti: %s", hid)

    async def heartbeat(self):
        while True:
            await asyncio.sleep(HEARTBEAT)
            for hid, (reader, writer) in list(self.waiting.items()):
                writer.write(b"K\n")
                if dead(reader, writer):   # yazma başarısızsa ya da Host kapanmışsa kayıt düşer
                    del self.waiting[hid]
                    writer.close()
                    log.info("host düşürüldü: %s", hid)


async def run(host, port, token):
    relay = Relay(token)
    port = await relay.start(host, port)
    print("Kamil V1 relay dinliyor: %s:%d" % (host, port), flush=True)
    print("Token: " + token, flush=True)
    print("Güvenlik duvarında TCP %d portunu açmayı unutma." % port, flush=True)
    if host in ("127.0.0.1", "localhost", "::1"):
        print("Yalnızca bu makineden erişilir; internete açmak için --host 0.0.0.0", flush=True)
    try:
        await relay.server.serve_forever()
    finally:
        await relay.stop()


def main():
    ap = argparse.ArgumentParser(description="Kamil V1 relay sunucusu")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--host", default="127.0.0.1", help="internete açmak için 0.0.0.0")
    ap.add_argument("--token", help="verilmezse rastgele üretilir ve ekrana yazılır")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    try:
        asyncio.run(run(args.host, args.port, resolve_token(args.token)))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        sys.exit("Dinlenemedi: %s" % e)


if __name__ == "__main__":
    main()
