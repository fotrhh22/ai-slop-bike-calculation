import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Mac USB/BLE 수신 + iPad 자전거 발전 대시보드")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    print("Mac: http://127.0.0.1:" + str(args.port))
    if args.host == "0.0.0.0":
        print("iPad: 같은 Wi-Fi에서 http://<Mac의 Wi-Fi IP>:" + str(args.port))
        print("신뢰하는 행사장 네트워크에서만 사용하세요. 인터넷 공개용 서버가 아닙니다.")
    uvicorn.run("bike.app:app", host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
