"""把各账号收到的私信汇总推送到同一个飞书机器人（企业自建应用）。

飞书侧全部走官方 SDK `lark-oapi`：发消息（im/v1/messages）、上传图片（im/v1/images）、
列群（im/v1/chats），token 由 SDK 自己缓存续期。SDK 是同步的，调用统一丢进线程池，
不阻塞事件循环；SDK 本身惰性导入（见 load_sdk）。只有下载闲鱼图片那一步仍用 httpx
（要自带 Referer，SDK 不管这个）。
"""

from __future__ import annotations

from functools import lru_cache
from io import BytesIO
from shutil import which
from subprocess import run as run_process
from tempfile import NamedTemporaryFile
from types import SimpleNamespace
from typing import TYPE_CHECKING

from anyio import to_thread
from httpx import AsyncClient, HTTPError, TimeoutException
from loguru import logger

from .goofish_utils import CONTENT_TYPE_LABELS, extract_mp4_duration_ms, to_milliseconds
from .sender import DEFAULT_HEADER_COLOR, build_card, build_text_message, content_json

if TYPE_CHECKING:
    from collections.abc import Sequence

    from lark_oapi import Client

    from .types import MessageInfo, MessageMedia

# 同一张图只上传一次；保留条数上限，避免长期运行无限增长
_MAX_IMAGE_CACHE = 200
# 单张图片下载上限（闲鱼原图一般几百 KB，给足余量）
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
# 飞书上传文件的硬限制（官方文档：不超过 30 MB）
_MAX_FILE_BYTES = 30 * 1024 * 1024
# 图片内嵌成功后要一并去掉的标注行：正文里那句 `[图片]` 的作用是告诉网页"这是图片"，
# 图片本身都显示出来了就不必再留一句标注
_IMAGE_LABEL_LINES = frozenset({f'[{CONTENT_TYPE_LABELS[2]}]'})
# 下载闲鱼图片/媒体要带 Referer/UA：不带的话部分地址直接返回 420（实测换头后 200）
IMAGE_DOWNLOAD_HEADERS = {
    'Referer': 'https://www.goofish.com/',
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
    ),
}
# 上传给飞书的文件名（MultipartEncoder 用它当 filename，扩展名要和 file_type 一致）
_IMAGE_FILENAME = 'message.jpg'
_MEDIA_FILENAMES = {'video': 'message.mp4', 'audio': 'message.opus'}
# 语音消息：飞书只收 OPUS，且音频要另发一条 audio 消息（卡片没有音频组件）
_AUDIO_FFMPEG_ARGS = ('-acodec', 'libopus', '-ac', '1', '-ar', '16000', '-f', 'ogg')


@lru_cache(maxsize=1)
def load_sdk() -> SimpleNamespace:
    """惰性加载飞书官方 SDK，返回用到的那些类。

    `import lark_oapi` 会连带把全部 API 的事件处理器都导一遍（它自己 `__init__` 里
    有 `from . import ws`），实测要 9~10 秒。于是放到真正要用时再导，Web 启动时
    在后台线程里预热一次（见 `__main__.run_web`），别让它落在启动路径或第一条消息上。
    """
    from lark_oapi import Client, LogLevel
    from lark_oapi.api.im.v1 import (
        CreateFileRequest,
        CreateFileRequestBody,
        CreateImageRequest,
        CreateImageRequestBody,
        CreateMessageRequest,
        CreateMessageRequestBody,
        ListChatRequest,
    )

    return SimpleNamespace(
        Client=Client,
        LogLevel=LogLevel,
        CreateFileRequest=CreateFileRequest,
        CreateFileRequestBody=CreateFileRequestBody,
        CreateImageRequest=CreateImageRequest,
        CreateImageRequestBody=CreateImageRequestBody,
        CreateMessageRequest=CreateMessageRequest,
        CreateMessageRequestBody=CreateMessageRequestBody,
        ListChatRequest=ListChatRequest,
    )


class NotifyError(RuntimeError):
    """推送失败（网络问题或飞书返回非 0 错误码）。"""


def format_direction(account_label: str, info: MessageInfo, sender: str = '') -> str:
    """消息流向：「发送方 → 接收方」，接收方写成「昵称(账号)」。"""
    who = sender or info.get('send_user_name') or info.get('send_user_id') or '未知用户'
    return f'{who} → {account_label}'


def build_image_markdown(image_key: str, alt: str = '图片') -> str:
    """卡片 markdown 里的内嵌图片。

    飞书只认自建应用上传得到的 image_key，外部 http 地址会被拒收整张卡片
    （实测 ErrCode 200570 invalid image keys），所以这里必须传 key。
    """
    return f'![{alt}]({image_key})'


def describe_response(response) -> str:
    """把 SDK 的失败响应整理成一行日志/异常文案。"""
    return f'code={response.code} msg={response.msg} log_id={response.get_log_id()}'


def drop_lines(text: str, values: set[str]) -> str:
    """去掉正文里已经被内嵌图片取代的那些地址行。"""
    return '\n'.join(line for line in text.splitlines() if line.strip() not in values)


class FeishuNotifier:
    """把消息发到飞书（企业自建应用，需要 app_id + app_secret + chat_id）。

    三个都齐了才会真正发送；缺任何一个都只是空操作（在网页上补齐即可）。
    """

    def __init__(self, app_id: str = '', app_secret: str = '', chat_id: str = '', timeout: float = 10.0) -> None:
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.chat_id = chat_id.strip()
        self.timeout = timeout
        self._client: Client | None = None
        self._media_client: AsyncClient | None = None
        self._image_keys: dict[str, str] = {}
        self._file_keys: dict[str, dict] = {}

    @property
    def can_upload_images(self) -> bool:
        return bool(self.app_id and self.app_secret)

    @property
    def can_send_via_app(self) -> bool:
        """能发消息的前提：应用凭据 + 目标群。"""
        return bool(self.can_upload_images and self.chat_id)

    def _get_client(self) -> Client:
        """官方 SDK 的客户端（同步），token 由它自己缓存续期。

        只在需要时创建：没配凭据时不构造，免得白拿一次 token。
        """
        if self._client is None:
            module = load_sdk()
            self._client = (
                module.Client.builder()
                .app_id(self.app_id)
                .app_secret(self.app_secret)
                .log_level(module.LogLevel.ERROR)  # SDK 自己会打日志，别灌进我们的日志里
                .build()
            )
        return self._client

    def _get_media_client(self) -> AsyncClient:
        """下载闲鱼图片专用的客户端（要带 Referer，SDK 不管这一步）。"""
        if self._media_client is None or self._media_client.is_closed:
            self._media_client = AsyncClient(headers=IMAGE_DOWNLOAD_HEADERS, timeout=self.timeout)
        return self._media_client

    async def close(self) -> None:
        if self._media_client is not None and not self._media_client.is_closed:
            await self._media_client.aclose()
        self._media_client = None
        self._client = None

    async def send(self, message: str) -> None:
        """纯文本推送（告警等不需要卡片格式的场景）。"""
        await self._send_message('text', content_json(build_text_message(message)))

    async def send_card(
        self,
        title: str,
        content: str,
        details: dict[str, str] | None = None,
        color: str = DEFAULT_HEADER_COLOR,
        images: Sequence[str] = (),
        media: MessageMedia | None = None,
    ) -> None:
        """富文本卡片：标题写流向，正文是普通文本，明细是前面的小号灰字（时间/商品名）。

        - images 里是图片地址：能上传成功的会内嵌显示（正文里对应的地址行与 `[图片]`
          标注一起去掉），传不上去的原样保留成可点链接；
        - media 是视频 / 语音：视频上传后内嵌进卡片的 video 组件（正文里的地址行与
          `[视频]` 标注一起去掉）；语音卡片放不下，先发卡片再补发一条语音消息
          （正文里只去掉地址行，保留 `[语音]` 标注，好和下面的语音对上）。
        传不上去的（格式不支持、超过 30MB、下载失败…）都退回原来那种可点链接。
        """
        body = await self._inline_images(content, images)
        uploaded = await self._prepare_media(media) if media else None
        if uploaded and media:
            body = self._strip_inlined_media(body, media, keep_label=uploaded['kind'] == 'audio')
        video = uploaded if uploaded and uploaded['kind'] == 'video' else None
        await self._send_message('interactive', content_json(build_card(title, body, details, color, video)))
        if uploaded and uploaded['kind'] == 'audio':
            payload = {'file_key': uploaded['file_key']}
            if uploaded['duration']:
                payload['duration'] = uploaded['duration']
            await self._send_message('audio', content_json(payload))

    def _strip_inlined_media(self, content: str, media: MessageMedia, *, keep_label: bool) -> str:
        """媒体已经发出去了，把正文里的地址行（以及可选的类型标注）去掉。"""
        dropped = {media['url']}
        if not keep_label:
            dropped.add(f'[{CONTENT_TYPE_LABELS[4 if media["kind"] == "video" else 3]}]')
        return '\n'.join(part for part in drop_lines(content, dropped).splitlines() if part)

    async def _prepare_media(self, media: MessageMedia) -> dict | None:
        """下载并上传视频 / 语音，返回 {'kind', 'file_key', 'duration', 'img_key'}。

        任何一步不成立都返回 None（调用方保留链接），不让消息丢掉。
        """
        if not self.can_upload_images:
            return None
        cached = self._file_keys.get(media['url'])
        if cached:
            return cached
        data = await self._download(media['url'])
        if data is None:
            return None
        duration = to_milliseconds(media['duration'])
        if media['kind'] == 'audio':
            data = await self._as_opus(data, media['url'])
            if data is None:
                return None
            file_type, file_name = 'opus', _MEDIA_FILENAMES['audio']
        else:
            # 闲鱼报文里的时长实测一直是 0（卡片会显示 00:00），所以从 mp4 里自己读一个
            duration = duration or extract_mp4_duration_ms(data)
            file_type, file_name = 'mp4', _MEDIA_FILENAMES['video']
        try:
            file_key = await self.upload_file(data, file_type=file_type, file_name=file_name, duration=duration)
        except NotifyError as e:
            logger.warning(f'上传媒体到飞书失败，改为只发链接: {e}')
            return None
        prepared: dict = {'kind': media['kind'], 'file_key': file_key, 'duration': duration, 'img_key': ''}
        if media['kind'] == 'video' and media['cover']:
            prepared['img_key'] = await self.resolve_image_key(media['cover']) or ''
        if len(self._file_keys) >= _MAX_IMAGE_CACHE:
            self._file_keys.pop(next(iter(self._file_keys)))
        self._file_keys[media['url']] = prepared
        return prepared

    async def _download(self, url: str) -> bytes | None:
        """下载媒体（闲鱼地址要带 Referer）。"""
        try:
            response = await self._get_media_client().get(url)
            response.raise_for_status()
            data = response.content
        except (HTTPError, TimeoutException, ValueError) as e:
            logger.warning(f'下载失败，改为只发链接: {url} ({type(e).__name__}: {e})')
            return None
        if not data or len(data) > _MAX_FILE_BYTES:
            logger.warning(f'文件大小异常（{len(data)} 字节，上限 {_MAX_FILE_BYTES}），改为只发链接: {url}')
            return None
        return data

    async def _as_opus(self, data: bytes, url: str) -> bytes | None:
        """把语音转成 OPUS —— 飞书只收这个格式。

        已经是 Ogg/Opus 就直接用；否则本机有 ffmpeg 就转一道；都没有就放弃（保留链接）。
        """
        if data[:4] == b'OggS' and b'OpusHead' in data[:256]:
            return data
        if which('ffmpeg') is None:
            logger.info(f'飞书只收 OPUS 语音，本机没有 ffmpeg 转不了，改为只发链接: {url}')
            return None
        try:
            return await to_thread.run_sync(self._transcode_with_ffmpeg, data)
        except NotifyError as e:
            logger.warning(f'语音转 OPUS 失败，改为只发链接: {e}')
            return None

    @staticmethod
    def _transcode_with_ffmpeg(data: bytes) -> bytes:
        """用 ffmpeg 把任意音频转成 16k 单声道 OPUS（同步，在线程里跑）。"""
        with NamedTemporaryFile(suffix='.src', delete=False) as source, NamedTemporaryFile(suffix='.opus') as target:
            source.write(data)
            source.flush()
            # 参数都是固定的，只有临时文件路径是变量
            result = run_process(
                [which('ffmpeg'), '-y', '-i', source.name, *_AUDIO_FFMPEG_ARGS, target.name],
                capture_output=True,
                check=False,
            )
            if result.returncode != 0:
                raise NotifyError(f'ffmpeg 退出码 {result.returncode}: {result.stderr[-200:]!r}')
            return target.read()

    async def upload_file(self, data: bytes, *, file_type: str, file_name: str, duration: int = 0) -> str:
        """上传文件（视频 mp4 / 语音 opus），返回 file_key。"""
        return await to_thread.run_sync(self._upload_file, data, file_type, file_name, duration)

    def _upload_file(self, data: bytes, file_type: str, file_name: str, duration: int) -> str:
        module = load_sdk()
        stream = BytesIO(data)
        stream.name = file_name
        body = module.CreateFileRequestBody.builder().file_type(file_type).file_name(file_name).file(stream)
        if duration:
            body = body.duration(duration)
        request = module.CreateFileRequest.builder().request_body(body.build()).build()
        response = self._get_client().im.v1.file.create(request)
        file_key = response.data.file_key if response.data else None
        if not response.success() or not file_key:
            raise NotifyError(f'上传 {file_type} 失败: {describe_response(response)}')
        return file_key

    async def _send_message(self, msg_type: str, content: str) -> None:
        """发一条消息（SDK 是同步的，整个「建请求 + 发」都丢线程池里跑）。"""
        if not self.can_send_via_app:
            logger.debug('未配置飞书应用凭据或目标群，跳过推送')
            return
        await to_thread.run_sync(self._create_message, msg_type, content)

    def _create_message(self, msg_type: str, content: str) -> None:
        module = load_sdk()
        request = (
            module.CreateMessageRequest.builder()
            .receive_id_type('chat_id')
            .request_body(
                module.CreateMessageRequestBody.builder()
                .receive_id(self.chat_id)
                .msg_type(msg_type)
                .content(content)
                .build()
            )
            .build()
        )
        response = self._get_client().im.v1.message.create(request)
        if not response.success():
            raise NotifyError(f'飞书推送失败: {describe_response(response)}')
        logger.debug('飞书推送成功')

    async def _inline_images(self, content: str, images: Sequence[str]) -> str:
        """把图片地址换成内嵌图片，返回新的正文。"""
        if not images or not self.can_upload_images:
            return content
        markdown: list[str] = []
        replaced: set[str] = set()
        for url in images:
            image_key = await self.resolve_image_key(url)
            if image_key:
                markdown.append(build_image_markdown(image_key))
                replaced.add(url)
        if not markdown:
            return content
        # 内嵌成功的图片，连同正文里那句 `[图片]` 标注一起去掉：图片就在眼前，不用再标一遍
        head = drop_lines(content, replaced | _IMAGE_LABEL_LINES)
        return '\n'.join(part for part in [head, *markdown] if part)

    async def resolve_image_key(self, url: str) -> str | None:
        """把图片地址换成飞书的 image_key（同一张图只上传一次）；失败返回 None。"""
        if not self.can_upload_images:
            return None
        cached = self._image_keys.get(url)
        if cached:
            return cached
        try:
            response = await self._get_media_client().get(url)
            response.raise_for_status()
            data = response.content
        except (HTTPError, TimeoutException, ValueError) as e:
            logger.warning(f'下载图片失败，改为只发链接: {url} ({type(e).__name__}: {e})')
            return None
        if not data or len(data) > _MAX_IMAGE_BYTES:
            logger.warning(f'图片大小异常（{len(data)} 字节），改为只发链接: {url}')
            return None
        try:
            image_key = await self.upload_image(data)
        except NotifyError as e:
            logger.warning(f'上传图片到飞书失败，改为只发链接: {e}')
            return None
        if len(self._image_keys) >= _MAX_IMAGE_CACHE:
            self._image_keys.pop(next(iter(self._image_keys)))
        self._image_keys[url] = image_key
        return image_key

    async def list_chats(self) -> list[dict[str, str]]:
        """机器人应用所在的群列表（界面上让用户挑一个当推送目标）。

        需要 `im:chat:readonly`（或 `im:chat`）权限，且机器人必须已经进群。
        """
        return await to_thread.run_sync(self._list_chats)

    def _list_chats(self) -> list[dict[str, str]]:
        module = load_sdk()
        request = module.ListChatRequest.builder().page_size(50).build()
        response = self._get_client().im.v1.chat.list(request)
        if not response.success():
            raise NotifyError(f'获取群列表失败: {describe_response(response)}')
        items = (response.data.items if response.data else None) or []
        return [
            {'chat_id': chat.chat_id or '', 'name': chat.name or chat.chat_id or ''} for chat in items if chat.chat_id
        ]

    async def upload_image(self, data: bytes) -> str:
        """上传图片，返回 image_key（卡片内嵌图片必须用它）。"""
        return await to_thread.run_sync(self._upload_image, data)

    def _upload_image(self, data: bytes) -> str:
        """上传图片（同步实现，在线程里跑）。

        SDK 会把 IO 对象原样交给 requests_toolbelt 做 multipart，所以这里包一个带
        `name` 的 BytesIO（文件名会出现在表单里）。
        """
        module = load_sdk()
        stream = BytesIO(data)
        stream.name = _IMAGE_FILENAME
        request = (
            module.CreateImageRequest.builder()
            .request_body(module.CreateImageRequestBody.builder().image_type('message').image(stream).build())
            .build()
        )
        response = self._get_client().im.v1.image.create(request)
        image_key = response.data.image_key if response.data else None
        if not response.success() or not image_key:
            raise NotifyError(f'上传图片失败: {describe_response(response)}')
        return image_key

    async def send_account_message(
        self,
        account_label: str,
        info: MessageInfo,
        text: str,
        sender: str = '',
        details: dict[str, str] | None = None,
        color: str = DEFAULT_HEADER_COLOR,
        images: Sequence[str] = (),
        media: MessageMedia | None = None,
    ) -> None:
        """推送一条账号收到的私信：标题是「发送方 → 接收方」，正文是消息内容，
        明细（时间 / 商品名）是正文前面的小号灰字，images / media 是能内嵌或补发的媒体。

        account_label 为接收账号的「昵称(账号)」，sender 为发送方昵称。
        """
        await self.send_card(format_direction(account_label, info, sender), text, details, color, images, media)
