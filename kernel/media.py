"""富媒体发送的领域异常。

为什么要单独定义：适配器把所有失败都包成 RuntimeError，插件只能回一句
「发送失败了」，用户看不出是"额度用完"还是"文件太大"。这两件事的处置
完全不同（前者等明天，后者换文件），必须在类型上区分开。

层级：插件只依赖本模块，不依赖任何具体适配器。
"""
from __future__ import annotations


class MediaError(Exception):
    """富媒体相关失败的总类。"""


class DailyLimitExceeded(MediaError):
    """平台每日发送文件容量上限（官方 40093002，不可重试）。

    官方文档只给了错误码，**没有公布具体数值**。第三方实现记录的是 2GB/天，
    仅供参考。撞上之后只能等次日重置。
    """

    def __init__(self, message: str = "今天的文件发送额度已用完") -> None:
        super().__init__(message)


class FileTooLarge(MediaError):
    """超过平台单文件硬限制（官方 850031，200MB）。分片上传也救不了。"""

    def __init__(self, message: str = "文件超过平台单文件上限") -> None:
        super().__init__(message)


class UploadFailed(MediaError):
    """其它上传失败（网络、COS、格式不支持等），可重试。"""
