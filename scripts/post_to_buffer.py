"""Buffer公式API（https://api.buffer.com, GraphQL）で投稿を予約する。
MODE=discover: 組織・チャンネル・入力の型を debug/ に書き出す（キーやトークンは書かない）
MODE=dry     : 予約対象を表示するだけ
MODE=post    : 今日（日本時間）の分を posts/library.json から選び、customScheduled で予約し、posts/sent.json に記録
人が毎週作業しなくても、ライブラリが続く限り毎日自動で動く（Bot）。足りなくなったら debug/needs_content.txt を書く
"""
import json, os, sys, urllib.request, datetime as dt
API = "https://api.buffer.com"
KEY = os.environ.get("BUFFER_API_KEY", "")
MODE = os.environ.get("MODE", "post")
BASE = os.environ.get("PAGES_BASE", "").rstrip("/")
JST = dt.timezone(dt.timedelta(hours=9))

def gql(query, variables=None):
    req = urllib.request.Request(API, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        out = json.loads(r.read())
    if out.get("errors"):
        raise RuntimeError(json.dumps(out["errors"], ensure_ascii=False))
    return out["data"]

def load(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except FileNotFoundError:
        return default

def channels():
    orgs = gql("{ account { organizations { id name } } }")["account"]["organizations"]
    res = []
    for o in orgs:
        q = "query($id: OrganizationId!){ channels(input:{organizationId:$id}){ id name service } }"
        try:
            res += gql(q, {"id": o["id"]})["channels"]
        except RuntimeError:
            q2 = "query($id: String!){ channels(input:{organizationId:$id}){ id name service } }"
            res += gql(q2, {"id": o["id"]})["channels"]
    return orgs, res

def type_info(name):
    q = """query($n:String!){ __type(name:$n){ name kind inputFields{ name type{ name kind ofType{ name kind ofType{ name kind } } } } enumValues{ name } } }"""
    return gql(q, {"n": name})["__type"]

def build_today(lib, state, today):
    """今日の投稿（channel, text, media, 時刻）を決める"""
    out = []
    start = dt.date.fromisoformat(lib["start"])
    i = (today - start).days
    if i < 0:
        return out
    sp = lib.get("special", {}).get(today.isoformat(), {})
    # X：毎日1件、ライブラリを順番に
    if i < len(lib["x"]):
        p = lib["x"][i]
        out.append(("x", p["text"], p.get("video") or p.get("image")))
    else:
        open("debug/needs_content.txt", "w", encoding="utf-8").write(f"{today} Xのライブラリが尽きました。追加してください\n")
    # Instagram・TikTok：決まった曜日に、YouTubeで公開済み（前日まで）で未使用のショート
    for ch in ("instagram", "tiktok"):
        if ch in sp:
            out.append((ch, sp[ch]["text"], sp[ch].get("media"))); continue
        days = lib["reel_weekdays"][ch] if isinstance(lib["reel_weekdays"], dict) else lib["reel_weekdays"]
        if today.weekday() not in days and not (ch == "tiktok" and i == 0):
            continue
        used = set(state.setdefault("used_reels", {}).setdefault(ch, []))
        cand = [r for r in lib["reels"] if r["short"] not in used and (not r.get("yt") or dt.date.fromisoformat(r["yt"]) < today)]
        if not cand:
            open("debug/needs_content.txt", "a", encoding="utf-8").write(f"{today} {ch}に使える新しいショートがありません\n")
            continue
        r = cand[0]
        out.append((ch, r[ch], r["video"], r["short"]))
    return out

def main():
    if not KEY:
        sys.exit("BUFFER_API_KEY が設定されていません（Settings → Secrets → Actions）")
    os.makedirs("debug", exist_ok=True)
    if MODE == "discover":
        orgs, chs = channels()
        info = {"organizations": [{"id": o["id"], "name": o["name"]} for o in orgs], "channels": chs, "types": {}}
        names = ["CreatePostInput", "AssetsInput", "AssetInput", "ImageAssetInput", "VideoAssetInput",
                 "PostInputMetaData", "InstagramPostMetadataInput", "TikTokPostMetadataInput", "TwitterPostMetadataInput",
                 "SchedulingType", "ShareMode", "PostType"]
        for n in names:
            try:
                info["types"][n] = type_info(n)
            except Exception as e:
                info["types"][n] = {"error": str(e)[:300]}
        mq = """{ __schema { mutationType { fields { name args { name type { name kind ofType { name kind } } } } } } }"""
        info["mutations"] = gql(mq)["__schema"]["mutationType"]["fields"]
        for n in ["DeletePostInput", "PostId"]:
            try:
                info["types"][n] = type_info(n)
            except Exception as e:
                info["types"][n] = {"error": str(e)[:300]}
        json.dump(info, open("debug/discover.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(json.dumps(info, ensure_ascii=False)[:3000]); return
    if MODE == "publish_now":
        # 予約済みの「対象日」の投稿を今すぐ公開し、記録の日付を今日に付け替える
        state = load("posts/sent.json", {})
        done = state.setdefault("done", {})
        target = os.environ.get("TODAY", "")
        real = dt.datetime.now(JST).date().isoformat()
        log = []
        for key in [k for k in list(done) if k.startswith(target)]:
            q = """mutation($input: PublishPostNowInput!){ publishPostNow(input:$input){ __typename } }"""
            try:
                r = gql(q, {"input": {"id": done[key]}})
                log.append(f"{key} 今すぐ公開: {r}")
                done[real + key[len(target):]] = done.pop(key)
            except Exception as e:
                log.append(f"失敗: {key} {str(e)[:400]}")
        print("\n".join(log))
        open("debug/last_run.txt", "w", encoding="utf-8").write(f"{dt.datetime.now(JST).isoformat()} publish_now\n" + "\n".join(log) + "\n")
        json.dump(state, open("posts/sent.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        return
    lib = load("posts/library.json", {})
    state = load("posts/sent.json", {})
    today = dt.datetime.now(JST).date()
    if os.environ.get("TODAY"):
        today = dt.date.fromisoformat(os.environ["TODAY"])
    done = state.setdefault("done", {})
    _, chs = channels()
    by_service = {}
    for c in chs:
        by_service.setdefault(c["service"].lower(), c["id"])
    log = []
    for item in build_today(lib, state, today):
        ch, text, media = item[0], item[1], item[2]
        key = f"{today.isoformat()}-{ch}"
        if key in done:
            continue
        hh, mm = lib["times"][ch].split(":")
        when = dt.datetime.combine(today, dt.time(int(hh), int(mm)), JST)
        now = dt.datetime.now(JST)
        if when < now + dt.timedelta(minutes=5):
            when = now + dt.timedelta(minutes=10)
        service = {"x": "twitter", "instagram": "instagram", "tiktok": "tiktok"}[ch]
        cid = by_service.get(service) or (by_service.get("x") if ch == "x" else None)
        if not cid:
            log.append(f"チャンネルなし: {ch}"); continue
        is_video = bool(media) and media.endswith(".mp4")
        inp = {"text": text, "channelId": cid, "schedulingType": "automatic", "mode": "customScheduled",
               "needsApproval": False, "assets": [],
               "dueAt": when.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")}
        if media:
            url = f"{BASE}/{media}"
            inp["assets"] = [{("video" if is_video else "image"): {"url": url}}]
        # AI生成の表示（規程3章11）を全媒体でオン
        if ch == "x":
            inp["metadata"] = {"twitter": {"isAiGenerated": True}}
        elif ch == "instagram":
            inp["metadata"] = {"instagram": {"type": "reel" if is_video else "post", "shouldShareToFeed": True, "isAiGenerated": True}}
        elif ch == "tiktok":
            inp["metadata"] = {"tiktok": {"isAiGenerated": True}}
        log.append(("[dry] " if MODE == "dry" else "") + f"{key} {when.isoformat()} {media}")
        if MODE == "dry":
            continue
        q = """mutation($input: CreatePostInput!){ createPost(input:$input){ __typename ... on PostActionSuccess { post { id } } ... on InvalidInputError { message } } }"""
        try:
            r = gql(q, {"input": inp})["createPost"]
        except Exception as e:
            log.append(f"失敗: {key} {str(e)[:500]}"); continue
        if r.get("__typename") == "PostActionSuccess":
            done[key] = r["post"]["id"]
            if len(item) > 3:
                state["used_reels"][ch].append(item[3])
        else:
            log.append(f"失敗: {key} {r}")
    print("\n".join(log))
    open("debug/last_run.txt", "w", encoding="utf-8").write(f"{dt.datetime.now(JST).isoformat()} {MODE}\n" + "\n".join(log) + "\n")
    if MODE == "post":
        json.dump(state, open("posts/sent.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
