"""Chinese/English for the viewer pages (the admin panel stays English).

Templates call `_("English text", name=value)`. The English text is the key; a missing
translation falls back to English, so nothing breaks if a string is not listed here.
"""
from __future__ import annotations

LANGS = ("zh", "en")
COOKIE = "pt_lang"

ZH = {
    # header
    "Search": "搜索",
    "Home": "首页",
    "Channels": "频道",
    "Admin": "管理",
    "Sign out": "退出",
    "Signed in as {name}": "已登录：{name}",
    "Signing out uses up this device's slot on your access code. Continue?":
        "退出后，本设备会占用访问码的一个设备名额，无法再用它登录。继续吗？",
    # sign in / access code / password
    "Sign in": "登录",
    "Your videos. Your server. Yours to keep.": "你的视频，你的服务器，永远属于你。",
    "Username": "用户名",
    "Password": "密码",
    "I have an access code": "我有访问码",
    "Access code": "访问码",
    "Enter access code": "输入访问码",
    "Code": "访问码",
    "Continue": "继续",
    "Each code works on a limited number of devices. This browser will stay signed in; "
    "clearing its cookies uses up another device slot.":
        "每个访问码只能在有限数量的设备上使用。本浏览器会保持登录；清除 Cookie 会再占用一个设备名额。",
    "Sign in with an account": "使用账号登录",
    "Too many failed attempts. Wait 15 minutes and try again.": "失败次数太多，请 15 分钟后再试。",
    "Wrong username or password.": "用户名或密码错误。",
    "This code is not valid.": "访问码无效。",
    "This code has expired.": "访问码已过期。",
    "This code has already been used on the maximum number of devices.": "此访问码的设备名额已用完。",
    "New password": "新密码",
    "Your new password": "你的新密码",
    "Passwords are changed every {months} months. Your old one no longer works after this step.":
        "密码每 {months} 个月更换一次。完成这一步后，旧密码将失效。",
    "Write it down on paper now.": "请现在把它抄在纸上。",
    "It is shown only once. Letters are case-sensitive; dashes are optional.":
        "它只显示这一次。字母区分大小写；短横线可以不输入。",
    "Type it again to confirm": "再输入一遍以确认",
    "I have written it down": "我已经抄好了",
    "That did not match. A NEW password has been generated below; write it down carefully.":
        "输入不一致。下面生成了一个新的密码，请仔细抄下来。",
    "Password changed. Other devices have been signed out.": "密码已更改，其他设备已退出登录。",
    # lists
    "Results for “{q}”": "“{q}”的搜索结果",
    "Nothing found.": "没有找到。",
    "No videos yet.": "还没有视频。",
    "No channels yet.": "还没有频道。",
    "{n} videos": "{n} 个视频",
    "updated {ago}": "{ago}更新",
    "About": "简介",
    "Short": "短视频",
    # watch
    "Description": "简介",
    "{n} comments": "{n} 条评论",
    "showing {n} saved": "显示已保存的 {n} 条",
    "pinned": "置顶",
    "1 reply": "1 条回复",
    "{n} replies": "{n} 条回复",
    "More from this channel": "该频道的更多视频",
    "Comments are updated daily during the first month.": "发布后一个月内，评论每天更新。",
    "Share": "分享",
    "Create a share code for this video? It works on 1 device for 3 days.":
        "为这个视频生成分享码？可在 1 台设备上使用 3 天。",
    "Share this video": "分享这个视频",
    "Devices": "设备数",
    "Valid for (days)": "有效天数",
    "Create code": "生成访问码",
    "Cancel": "取消",
    "Share code": "分享码",
    "Send the link, or the code with the address below. It opens only this video, on {n} device(s), until {date}.":
        "把链接发给对方，或者把访问码和下面的网址一起发。它只能打开这个视频，可在 {n} 台设备上使用，有效期到 {date}。",
    "Link": "链接",
    "Copy": "复制",
    "Copied": "已复制",
    "Back to the video": "返回视频",
    "You have created many share codes today. Try again tomorrow.": "今天生成的分享码太多了，请明天再试。",
    "Continue watching": "继续观看",
    "History": "历史",
    "Latest": "最新",
    "Clear history": "清除记录",
    "Nothing watched yet.": "还没有观看记录。",
    "Clear your watch history? Where you stopped in each video is forgotten too.": "清除观看记录？每个视频的播放进度也会一并清除。",
    "Subtitles": "字幕",
    "Off": "关闭",
    "Chinese": "中文",
    "Chinese (Traditional)": "繁体中文",
    "English": "英文",
    "auto": "自动",
    # time
    "just now": "刚刚",
    "{n} year ago": "{n}年前", "{n} years ago": "{n}年前",
    "{n} month ago": "{n}个月前", "{n} months ago": "{n}个月前",
    "{n} week ago": "{n}周前", "{n} weeks ago": "{n}周前",
    "{n} day ago": "{n}天前", "{n} days ago": "{n}天前",
    "{n} hour ago": "{n}小时前", "{n} hours ago": "{n}小时前",
    "{n} minute ago": "{n}分钟前", "{n} minutes ago": "{n}分钟前",
}


def pick(cookie: str | None, accept_language: str | None) -> str:
    """Language from the pt_lang cookie, else the browser's first preference (default English)."""
    if cookie in LANGS:
        return cookie
    first = (accept_language or "").lower().split(",")[0].strip()
    return "zh" if first.startswith("zh") else "en"


def gettext(lang: str, text: str, **kw) -> str:
    s = ZH.get(text, text) if lang == "zh" else text
    return s.format(**kw) if kw else s
