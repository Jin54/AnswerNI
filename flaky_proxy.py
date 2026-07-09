"""
127.0.0.1:8101 -> 127.0.0.1:8100 TCP 프록시.
/events/ 경로 요청에 한해: 전역 카운터가 임계치 이하이면 응답 헤더만 흘리고 즉시 커넥션을 끊어
EventSource 의 onerror(진짜 네트워크 순단)를 재현한다. 그 외 요청(/, /ask, /static)은 완전 패스스루.
"""
import asyncio

UPSTREAM_HOST, UPSTREAM_PORT = "127.0.0.1", 8100
LISTEN_PORT = 8101

# 시나리오 스위치: env로 제어
import os
MODE = os.environ.get("FLAKY_MODE", "cut5")  # "cut5"=5회 연속 완전절단(6번째부터 정상) / "cut1"=1회만 절단 후 정상
_cut_count = {"n": 0}

async def handle(reader, writer):
    try:
        req_line = await reader.readline()
        headers = b""
        while True:
            line = await reader.readline()
            headers += line
            if line in (b"\r\n", b""):
                break
        path = req_line.split(b" ")[1] if len(req_line.split(b" ")) > 1 else b""
        is_events = path.startswith(b"/events/")

        up_r, up_w = await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
        up_w.write(req_line + headers)
        await up_w.drain()

        if is_events:
            limit = 5 if MODE == "cut5" else 1
            if _cut_count["n"] < limit:
                _cut_count["n"] += 1
                # 헤더까지만 잠깐 흘리고(연결 성립은 확인시킴) 짧게 대기 후 강제 종료 -> onerror 유발
                await asyncio.sleep(0.3)
                writer.close()
                up_w.close()
                return
        # 정상 패스스루 (양방향 relay)
        async def pipe(r, w):
            try:
                while True:
                    data = await r.read(4096)
                    if not data:
                        break
                    w.write(data)
                    await w.drain()
            except Exception:
                pass
            finally:
                try: w.close()
                except Exception: pass
        await asyncio.gather(pipe(reader, up_w), pipe(up_r, writer))
    except Exception as e:
        print("proxy error:", e)
    finally:
        try: writer.close()
        except Exception: pass

async def main():
    server = await asyncio.start_server(handle, "127.0.0.1", LISTEN_PORT)
    print(f"flaky proxy on :{LISTEN_PORT} -> :{UPSTREAM_PORT} MODE={MODE}")
    async with server:
        await server.serve_forever()

if __name__ == "__main__":
    asyncio.run(main())
