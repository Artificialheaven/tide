基于 WebSocket 协议的单向流式合成接口，一次性输入文本，流式返回音频，支持中、英、日、西等多语种及多种方言口音。

**运行依赖文件**

**Python**

[websocket unidirectional.zip](https://portal.volccdn.com/obj/volcfe/cloud-universal-doc/upload_42a6c874fbeda2e99b2795bce84fa30d.zip)

**Java**

[websocket unidirectional.zip](https://portal.volccdn.com/obj/volcfe/cloud-universal-doc/upload_321ac6b07f43505fd41e2997fc15ceea.zip)

---

WSS`wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream`

### 请求头

**X\-Api\-Key ** `string` 必选

API Key 可从 [控制台>API Key管理](https://console.volcengine.com/speech/new/setting/apikeys?projectName=default.) 获取

> **说明**

> 同时支持[旧版控制台](https://console.volcengine.com/speech/service/10035)的鉴权方式，详见[旧版控制台鉴权参考](https://docs.volcengine.com/docs/6561/2534847?lang=zh)

**X\-Api\-Resource\-Id ** `string` 必选

请求的模型版本，可选值：

* `seed-tts-2.0`:豆包语音合成大模型2.0，支持使用[豆包语音合成模型2.0音色](https://docs.volcengine.com/docs/6561/1257544?lang=zh#%E8%B1%86%E5%8C%85%E8%AF%AD%E9%9F%B3%E5%90%88%E6%88%90%E6%A8%A1%E5%9E%8B2-0%E3%80%81s2s-o2-0%E3%80%81-s2s-%E5%85%A8%E5%8F%8C%E5%B7%A5-%E9%9F%B3%E8%89%B2%E5%88%97%E8%A1%A8)

* `seed-icl-2.0`:豆包声音复刻大模型2.0，支持使用声音复刻接口克隆的音色，具体音色详见[控制台>音色库](https://console.volcengine.com/speech/new/voices?projectName=default)

**X\-Api\-Request\-Id ** `string` 必选

标识客户端请求ID，uuid随机字符串

**X\-Control\-Require\-Usage\-Tokens\-Return ** `string`

若设置为`*`，将返回计费的字符数

### 请求体

**req_params ** `object` 必选

**text ** `string` 必选

输入待合成的文本

**model** `string`

指定模型版本，默认值为`seed-tts-2.0-standard`

> **说明**

> 仅当`speaker`参数为复刻音色时需指定此参数，且指定后不支持使用语音指令`context_texts`

**speaker** `string` 必选

指定音色 ID，具体的音色ID可从[控制台 > 音色库](https://console.volcengine.com/speech/new/voices?projectName=default)获取

**ssml** `string`

SSML标记文本，启用后将按SSML规则解析 `text`。SSML规则请参考：[SSML标记语言](https://docs.volcengine.com/docs/6561/1330194?lang=zh)

> **说明**

* > 目前仅中英文音色支持ssml，音色详情请参考：[豆包语音合成模型2.0音色](https://docs.volcengine.com/docs/6561/1257544?lang=zh#%E8%B1%86%E5%8C%85%E8%AF%AD%E9%9F%B3%E5%90%88%E6%88%90%E6%A8%A1%E5%9E%8B2-0%E3%80%81s2s-o2-0%E3%80%81-s2s-%E5%85%A8%E5%8F%8C%E5%B7%A5-%E9%9F%B3%E8%89%B2%E5%88%97%E8%A1%A8)

* > 启用后需将`disable_markdown_filter`设置为`false`，否则此参数不生效

**audio_params** `object` 必选

音频参数

**format** `string`

指定音频格式，默认值为`mp3`，支持 `mp3` / `pcm` / `ogg_opus` / `wav`

> **说明**

> 流式场景推荐使用`pcm`，不建议使用`wav`

**sample_rate** `int`

指定输出音频的采样率，单位为Hz，不同音频格式支持的采样率不同，详细如下：

* `wav`/`pcm`/`mp3`默认值为`24000`，取值范围为[`8000`,`16000`,`22050`，`24000`,`32000`,`44100`,`48000`]

* `ogg_opus`仅支持`48000`

**bit_rate** `int`

指定音频比特率，单位 bps，不同音频格式对比特率支持的情况不同，详细如下：

* `mp3`：默认为`64000` ，可选值[`64000`,`160000`]

* `ogg_opus`：可选值[`64000`,`160000`]

* `wav`/`pcm`：不支持指定比特率

> **说明**

> 当设置`disable_default_bit_rate`为`true`后，可选值[`16000`,`32000`,`64000`,`160000`]

**speech_rate** `int`

指定音频的语速，默认值为`0`，取值范围 [`-50`, `100`]。其中，取值`100`代表2.0倍速，`-50`代表0.5倍速

**loudness_rate** `int`

指定音频的音量，默认值为`0`，取值范围 [`-50`, `100`]。其中，取值`100`代表2.0倍音量，`-50`代表0.5倍音量

**enable_subtitle** `bool`

启用字幕服务，开启后将返回字级别的时间戳，默认值为`false`

> **说明**

> 仅支持中文和英文语种

**additions** `string`

**max_length_to_filter_parenthesis ** `int`

指定过滤括号内文本的长度，单位为字符，默认为0（即不过滤），推荐取值范围0~100

> **说明**

* > 该参数用于过滤文本中括号内的注释、补充说明等无需朗读的内容

* > 当括号内文本字符数超过设定值时，该括号的过滤功能将失效，括号内内容会被正常朗读。若存在较长的括号文本需要过滤，建议客户端在送入合成前自行完成前置过滤，以避免过滤失效及不必要的时延

**silence_duration** `int`

配置文本末尾的静音时长，单位 ms，默认值为`0`，取值范围为[`0`,`30000`]

**disable_markdown_filter** `bool`

Markdown解析过滤参数，默认值为`false`

* `true`：启用后，系统会解析并去除Markdown语法，例如" \*\*你好\*\* "朗读为 "你好"

* `false`：停用后，系统会保留原始字符，例如 " \*\*你好\*\* " 朗读为 "星星你好星星"

**disable_emoji_filter** `bool`

Emoji解析过滤参数，默认值为`false`

* `true`：启用后，系统会解析并去除文本中的Emoji符号

* `false`：停用后，系统会保留文本中的Emoji符号

**latex_parser** `string`

启用Latex文本朗读能力功能。可取值：`v2`

> **说明**

* > 该参数适用于教育场景，启用后，会增加一定的时延。

* > 启用该参数时，需同时将`disable_markdown_filter`设置为`true`

**explicit_language** `string`

指定朗读语种。开启后，仅朗读指定语种的文本，其他语种的内容会被跳过或合成失败，支持的语种如下：

* `zh-cn`：中文为主，支持中英混读

* `en`：英语

* `ja`：日语

* `es-mx`：墨西哥西语

* `id`：印度尼西亚语

* `pt-br`：巴西葡萄牙语

* `pt`：葡萄牙语

* `ko`：韩语

* `it`：意大利语

* `de`：德语

* `fr`：法语

* `th`：泰语

* `vi`：越南语

* `ru`：俄语

* `fil`：菲律宾语

* `ms`：马来语

* `ar`：阿拉伯语

* `pl`：波兰语

* `tr`：土耳其语

* `sv`：瑞典语

* `nl`: 荷兰语

* `no`: 挪威

* `uk`: 乌克兰语

* `fi`: 芬兰

* `da`: 丹麦

* `cs`: 捷克

* `hu`: 匈牙利

* `el`: 希腊

* `ro`: 罗马尼亚

* `hi`: 印度

* `es-es`: 西班牙西语

> **说明**

* > 输入文本语种须与 `explicit_language` 指定的语种一致，不一致时合成效果不予保证

* > [豆包语音合成模型 2.0 音色列表](https://docs.volcengine.com/docs/6561/1257544?lang=zh#%E8%B1%86%E5%8C%85%E8%AF%AD%E9%9F%B3%E5%90%88%E6%88%90%E6%A8%A1%E5%9E%8B2-0%E3%80%81s2s-o2-0%E3%80%81-s2s-%E5%85%A8%E5%8F%8C%E5%B7%A5-%E9%9F%B3%E8%89%B2%E5%88%97%E8%A1%A8)中，仅命名为zh_*xxx*_*xxxx*_uranus_bigtts的音色支持`nl`、`no`、`uk`、`fi`、`da`、`cs`、`hu`、`el`、`ro`、`hi`、`es-es`等语种

**enable_auto_language_recognition**`bool`

开启自动多语种识别，默认为`false`，开启后，系统将根据首句文本自动识别语种，并使用对应语种进行朗读

> **说明**

* > 指定语种的优先级高于自动多语种识别，当同时指定了`explicit_language`时，自动多语种识别不生效

* > 支持的语种与 `explicit_language` 一致（`pt`与`es-es`除外）

* > 自动识别语种时，葡萄牙语文本默认输出 `pt-br`（巴西口音），西班牙语文本默认输出 `es-mx`（墨西哥口音）。如需使用欧洲葡萄牙语`pt`或西班牙西班牙语`es-es`，须通过 `explicit_language` 参数显式指定

* > 中英模型（中文、英文）与小语种模型（日、韩、法语、西语等小语种）相互独立，各自仅对对应语种合成效果最佳

**explicit_dialect** `string`

指定方言。

* `beijing`：北京话

* `dongbei`：东北话

* `henan`：河南话

* `shaanxi`：陕西话

* `shanghai`：上海话

* `sichuan`：四川话

* `tianjin`：天津话

* `yue`：粤语

> **说明**

> 使用该参数时，`speaker`需要设置支持方言的音色。音色的详细情况请参见：[音色列表](https://docs.volcengine.com/docs/6561/1257544?lang=zh)

**aigc_watermark** `bool`

启用AIGC生成标识，默认值：`false`。开启后，将在音频合成结尾添加节奏标识。

**aigc_metadata** `object`

在合成音频中添加meta水印，支持音频格式 `mp3` / `wav` / `ogg_opus`

**enable ** `bool`

启用meta隐式水印，默认值：`false`

**content_producer ** `string`

指定合成服务提供者的名称或编码

**produce_id ** `string`

指定自定义的内容制作编号

**content_propagator ** `string`

指定内容传播服务提供者的名称或编码

**propagate_id ** `string`

指定自定义的内容传播编号

**post_process** `object`

后处理相关配置

**pitch ** `int`

指定音调，默认值为0，默认不调整音调，取值范围：[\-12,12]

取值越大，音调越高（声音越尖锐、越明亮）；取值越小，音调越低（声音越低沉，越厚重）

**context_texts** `array`

配置语音指令

示例：

```Python
"context_texts":[ "你可以用特别特别痛心的语气说话吗?"]
```

> **说明**

* > 仅`speaker`参数设置为[豆包语音合成模型2.0音色](https://docs.volcengine.com/docs/6561/1257544?lang=zh#%E8%B1%86%E5%8C%85%E8%AF%AD%E9%9F%B3%E5%90%88%E6%88%90%E6%A8%A1%E5%9E%8B2-0%E3%80%81s2s-o2-0%E3%80%81-s2s-%E5%85%A8%E5%8F%8C%E5%B7%A5-%E9%9F%B3%E8%89%B2%E5%88%97%E8%A1%A8)时，支持使用语音指令

* > 该字段文字不参与计费

**section_id ** `string`

配置段落标识，可用于保持跨包语义

> **说明**

> 该参数仅[豆包语音合成模型2.0音色](https://docs.volcengine.com/docs/6561/1257544?lang=zh#%E8%B1%86%E5%8C%85%E8%AF%AD%E9%9F%B3%E5%90%88%E6%88%90%E6%A8%A1%E5%9E%8B2-0%E3%80%81s2s-o2-0%E3%80%81-s2s-%E5%85%A8%E5%8F%8C%E5%B7%A5-%E9%9F%B3%E8%89%B2%E5%88%97%E8%A1%A8)、豆包声音复刻大模型2.0音色支持

**tone_fidelity ** **`bool`**

开启还原模式，开启后模型将尽可能还原送入的prompt音频音色和说话风格（情感、韵律、口音等）

默认值：`false`

> **说明**

* > **仅适用于**豆包声音复刻大模型2.0音色

* > **仅支持**合成和上传音频同语种的文本**，不支持**跨语种合成**，不支持**双向流合成接口

**pronunciation_dict**`object`

配置发音词典，修正指定文本的发音

**tone**`object`

配置发音规则数组，支持发音修正与文本转写两类规则

* 发音修正：`原词/(拼音音节)`，用于指定词汇的具体发音

* 文本转写：`原词/目标文本`，用于将原词替换为目标文本后再合成

 示例：

```Python
 "req_params": {
    "text": "我在北京说 omg",
    "speaker": "zh_female_vv_uranus_bigtts",
    "audio_params": {
      "format": "mp3",
      "sample_rate": 24000
    },
    "additions": "{\"pronunciation_dict\":{\"tone\":[\"北京/(bei3)(jing1)\",\"omg/oh my god\"]}}" 
  }
```

> **说明**

* > `tone`中每条规则采用`原词/修正内容`格式，以`/`分隔。服务端按文本从左到右进行贪心最长匹配，存在多个可匹配词条时，优先匹配更长的原词

* > 最多支持配置 5000 个词条；每个原词长度不超过 9 个字符，不能为空、不能包含空格、不可重复

* > 仅豆包语音合成大模型2.0、豆包语音复刻大模型2.0对应的中英文音色支持

* > 命中发音字典规则的文本片段不支持 SSML 标签，同时使用时 SSML 标签将失效，建议二者择一

### 响应

**X\-Tt\-Logid ** `string`

服务端返回的 logid，用于在咨询或者反馈时定位问题

**EventType** `string`

响应事件类型。包含以下几种事件：

* `TTSSentenceStart`：开始合成音频

* `TTSResponse`：音频合成内容

* `TTSSentenceEnd`：音频合成结束

* `TTSSubtitle`：返回音频合成字幕

* `SessionFinished`：会话结束

**MsgType ** `string`

消息类型。响应消息类型有以下两种

* `FullServerResponse`：全量服务端响应

* `AudioOnlyServer`：仅音频服务端响应

**SessionId ** `string`

会话 ID，用于标识一次合成会话

**PayloadSize ** `int`

返回的音频片段大小，单位是字节

**Payload ** `object`

当前事件携带的响应内容

**phonemes ** `object`

音素相关时间戳

**text ** `string`

合成音频文本

**words ** `object`

字级时间戳列表

**confidence ** `float`

时间戳置信度，范围 0~1

**startTime ** `float`

开始时间（秒）

**endTime ** `float`

结束时间（秒）

**word ** `string`

字

**usage ** `object`

本次请求的资源消耗统计

**text_words ** `int`

本次请求计费的文本字数（含标点）

```mixin-react
return (<ApiSample data={"{\"type\":\"multiple\",\"config\":{\"inputName\":\"输入示例\",\"outputName\":\"输出示例\"},\"data\":[{\"title\":\"Python\",\"key\":\"0\",\"inputCode\":\"import asyncio\\nimport json\\nimport logging\\nimport uuid\\n\\nimport websockets\\n\\nfrom protocols import EventType, MsgType, full_client_request, receive_message\\n\\nlogging.basicConfig(level=logging.INFO)\\nlogger = logging.getLogger(__name__)\\n\\nasync def main() -> None:\\n    headers = {\\n        \\\"X-Api-Key\\\": \\\"your_api_key\\\",\\n        \\\"X-Api-Resource-Id\\\": \\\"seed-tts-2.0\\\",\\n        \\\"X-Api-Request-Id\\\": str(uuid.uuid4()),\\n        \\\"X-Control-Require-Usage-Tokens-Return\\\": \\\"*\\\"\\n    }\\n\\n    body = {\\n        \\\"req_params\\\": {\\n            \\\"speaker\\\": \\\"zh_female_gaolengyujie_uranus_bigtts\\\",\\n            \\\"text\\\": \\\"你好\\\",\\n            \\\"audio_params\\\": {\\n                \\\"format\\\": \\\"mp3\\\",\\n                \\\"sample_rate\\\": 24000,\\n            }\\n        }\\n    }\\n\\n    websocket = await websockets.connect(\\n        \\\"wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream\\\",\\n        additional_headers=headers,\\n        max_size=10 * 1024 * 1024,\\n    )\\n    logger.info(\\n        f\\\"Connected to WebSocket server, Logid: {websocket.response.headers.get('x-tt-logid', '')}\\\"\\n    )\\n\\n    try:\\n        await full_client_request(websocket, json.dumps(body).encode())\\n\\n        audio_data = bytearray()\\n        while True:\\n            msg = await receive_message(websocket)\\n\\n            if msg.type == MsgType.FullServerResponse and msg.event == EventType.SessionFinished:\\n                break\\n            if msg.type == MsgType.AudioOnlyServer and msg.payload:\\n                audio_data.extend(msg.payload)\\n            elif msg.type == MsgType.Error:\\n                raise RuntimeError(f\\\"TTS conversion failed: {msg}\\\")\\n\\n        if not audio_data:\\n            raise RuntimeError(\\\"No audio data received\\\")\\n\\n        with open(\\\"unidirectional_stream.mp3\\\", \\\"wb\\\") as f:\\n            f.write(audio_data)\\n        logger.info(f\\\"Audio received: {len(audio_data)}, saved to unidirectional_stream.mp3\\\")\\n    finally:\\n        await websocket.close()\\n        logger.info(\\\"Connection closed\\\")\\n\\nif __name__ == \\\"__main__\\\":\\n    asyncio.run(main())\",\"outputCode\":\"{\\n  \\\"Logid\\\": \\\"20260616211035105B13415E264F69E61D\\\",\\n  \\\"Received\\\": [\\n    {\\n      \\\"MsgType\\\": \\\"FullServerResponse\\\",\\n      \\\"EventType\\\": \\\"TTSSentenceStart\\\",\\n      \\\"SessionId\\\": \\\"76ad2906-71c4-4b0b-8655-a77d2718b078\\\",\\n      \\\"Payload\\\": {\\n        \\\"phonemes\\\": [],\\n        \\\"text\\\": \\\"\\\",\\n        \\\"words\\\": []\\n      }\\n    },\\n    {\\n      \\\"MsgType\\\": \\\"AudioOnlyServer\\\",\\n      \\\"EventType\\\": \\\"TTSResponse\\\",\\n      \\\"SessionId\\\": \\\"76ad2906-71c4-4b0b-8655-a77d2718b078\\\",\\n      \\\"PayloadSize\\\": 2541\\n    },\\n    {\\n      \\\"MsgType\\\": \\\"FullServerResponse\\\",\\n      \\\"EventType\\\": \\\"TTSSentenceEnd\\\",\\n      \\\"SessionId\\\": \\\"76ad2906-71c4-4b0b-8655-a77d2718b078\\\",\\n      \\\"Payload\\\": {\\n        \\\"phonemes\\\": [],\\n        \\\"text\\\": \\\"你好\\\",\\n        \\\"words\\\": []\\n      }\\n    },\\n    {\\n      \\\"MsgType\\\": \\\"FullServerResponse\\\",\\n      \\\"EventType\\\": \\\"TTSSubtitle\\\",\\n      \\\"SessionId\\\": \\\"76ad2906-71c4-4b0b-8655-a77d2718b078\\\",\\n      \\\"Payload\\\": {\\n        \\\"phonemes\\\": [],\\n        \\\"text\\\": \\\"你好\\\",\\n        \\\"words\\\": [\\n          {\\n            \\\"confidence\\\": 0.6805309,\\n            \\\"endTime\\\": 0.285,\\n            \\\"startTime\\\": 0.155,\\n            \\\"word\\\": \\\"你\\\"\\n          },\\n          {\\n            \\\"confidence\\\": 0.9384748,\\n            \\\"endTime\\\": 0.735,\\n            \\\"startTime\\\": 0.285,\\n            \\\"word\\\": \\\"好\\\"\\n          }\\n        ]\\n      }\\n    },\\n    {\\n      \\\"MsgType\\\": \\\"FullServerResponse\\\",\\n      \\\"EventType\\\": \\\"SessionFinished\\\",\\n      \\\"SessionId\\\": \\\"76ad2906-71c4-4b0b-8655-a77d2718b078\\\",\\n      \\\"Payload\\\": {\\n        \\\"usage\\\": {\\n          \\\"text_words\\\": 2\\n        }\\n      }\\n    }\\n  ]\\n}\",\"inputLanguage\":\"\",\"outputLanguage\":\"\"},{\"title\":\"Java\",\"key\":\"1\",\"inputCode\":\"import com.speech.protocol.EventType;\\nimport com.speech.protocol.Message;\\nimport com.speech.protocol.MsgType;\\nimport com.speech.protocol.SpeechWebSocketClient;\\n\\nimport java.io.ByteArrayOutputStream;\\nimport java.net.URI;\\nimport java.nio.charset.StandardCharsets;\\nimport java.nio.file.Files;\\nimport java.nio.file.Path;\\nimport java.util.Map;\\nimport java.util.UUID;\\n\\npublic class ws单向流式 {\\n    private static final String ENDPOINT = \\\"wss://openspeech.bytedance.com/api/v3/tts/unidirectional/stream\\\";\\n    private static final String API_KEY = \\\"your_api_key\\\";\\n    private static final String RESOURCE_ID = \\\"seed-tts-2.0\\\";\\n\\n    public static void main(String[] args) throws Exception {\\n        Map<String, String> headers = Map.of(\\n                \\\"X-Api-Key\\\", API_KEY,\\n                \\\"X-Api-Resource-Id\\\", RESOURCE_ID,\\n                \\\"X-Api-Request-Id\\\", UUID.randomUUID().toString(),\\n                \\\"X-Control-Require-Usage-Tokens-Return\\\", \\\"*\\\");\\n\\n        String requestJson = \\\"{\\\"\\n                + \\\"\\\\\\\"req_params\\\\\\\":{\\\"\\n                + \\\"\\\\\\\"speaker\\\\\\\":\\\\\\\"zh_female_gaolengyujie_uranus_bigtts\\\\\\\",\\\"\\n                + \\\"\\\\\\\"text\\\\\\\":\\\\\\\"你好\\\\\\\",\\\"\\n                + \\\"\\\\\\\"audio_params\\\\\\\":{\\\"\\n                + \\\"\\\\\\\"format\\\\\\\":\\\\\\\"mp3\\\\\\\",\\\"\\n                + \\\"\\\\\\\"sample_rate\\\\\\\":24000,\\\"\\n                + \\\"\\\\\\\"enable_subtitle\\\\\\\":true\\\"\\n                + \\\"}\\\"\\n                + \\\"}\\\"\\n                + \\\"}\\\";\\n\\n        SpeechWebSocketClient client = new SpeechWebSocketClient(new URI(ENDPOINT), headers);\\n\\n        try {\\n            client.connectBlocking();\\n            client.sendFullClientMessage(requestJson.getBytes(StandardCharsets.UTF_8));\\n\\n            ByteArrayOutputStream audioStream = new ByteArrayOutputStream();\\n            while (true) {\\n                Message msg = client.receiveMessage();\\n\\n                if (msg.getType() == MsgType.AUDIO_ONLY_SERVER && msg.getPayload() != null) {\\n                    audioStream.write(msg.getPayload());\\n                    continue;\\n                }\\n\\n                if (msg.getType() == MsgType.ERROR) {\\n                    throw new RuntimeException(\\\"TTS conversion failed: \\\"\\n                            + new String(msg.getPayload(), StandardCharsets.UTF_8));\\n                }\\n\\n                if (msg.getType() == MsgType.FULL_SERVER_RESPONSE\\n                        && msg.getEvent() == EventType.TTS_SENTENCE_END) {\\n                    continue;\\n                }\\n\\n                if (msg.getType() == MsgType.FULL_SERVER_RESPONSE\\n                        && msg.getEvent() == EventType.SESSION_FINISHED) {\\n                    break;\\n                }\\n            }\\n\\n            if (audioStream.size() == 0) {\\n                throw new RuntimeException(\\\"No audio data received\\\");\\n            }\\n\\n            Path outputFile = Path.of(\\\"unidirectional_stream.mp3\\\");\\n            Files.write(outputFile, audioStream.toByteArray());\\n            System.out.printf(\\\"Audio received: %d, saved to %s%n\\\", audioStream.size(), outputFile);\\n        } finally {\\n            client.closeBlocking();\\n            System.out.println(\\\"Connection closed\\\");\\n        }\\n    }\\n}\",\"outputCode\":\"{\\n  \\\"Logid\\\": \\\"20260616211035105B13415E264F69E61D\\\",\\n  \\\"Received\\\": [\\n    {\\n      \\\"MsgType\\\": \\\"FullServerResponse\\\",\\n      \\\"EventType\\\": \\\"TTSSentenceStart\\\",\\n      \\\"SessionId\\\": \\\"76ad2906-71c4-4b0b-8655-a77d2718b078\\\",\\n      \\\"Payload\\\": {\\n        \\\"phonemes\\\": [],\\n        \\\"text\\\": \\\"\\\",\\n        \\\"words\\\": []\\n      }\\n    },\\n    {\\n      \\\"MsgType\\\": \\\"AudioOnlyServer\\\",\\n      \\\"EventType\\\": \\\"TTSResponse\\\",\\n      \\\"SessionId\\\": \\\"76ad2906-71c4-4b0b-8655-a77d2718b078\\\",\\n      \\\"PayloadSize\\\": 2541\\n    },\\n    {\\n      \\\"MsgType\\\": \\\"FullServerResponse\\\",\\n      \\\"EventType\\\": \\\"TTSSentenceEnd\\\",\\n      \\\"SessionId\\\": \\\"76ad2906-71c4-4b0b-8655-a77d2718b078\\\",\\n      \\\"Payload\\\": {\\n        \\\"phonemes\\\": [],\\n        \\\"text\\\": \\\"你好\\\",\\n        \\\"words\\\": []\\n      }\\n    },\\n    {\\n      \\\"MsgType\\\": \\\"FullServerResponse\\\",\\n      \\\"EventType\\\": \\\"TTSSubtitle\\\",\\n      \\\"SessionId\\\": \\\"76ad2906-71c4-4b0b-8655-a77d2718b078\\\",\\n      \\\"Payload\\\": {\\n        \\\"phonemes\\\": [],\\n        \\\"text\\\": \\\"你好\\\",\\n        \\\"words\\\": [\\n          {\\n            \\\"confidence\\\": 0.6805309,\\n            \\\"endTime\\\": 0.285,\\n            \\\"startTime\\\": 0.155,\\n            \\\"word\\\": \\\"你\\\"\\n          },\\n          {\\n            \\\"confidence\\\": 0.9384748,\\n            \\\"endTime\\\": 0.735,\\n            \\\"startTime\\\": 0.285,\\n            \\\"word\\\": \\\"好\\\"\\n          }\\n        ]\\n      }\\n    },\\n    {\\n      \\\"MsgType\\\": \\\"FullServerResponse\\\",\\n      \\\"EventType\\\": \\\"SessionFinished\\\",\\n      \\\"SessionId\\\": \\\"76ad2906-71c4-4b0b-8655-a77d2718b078\\\",\\n      \\\"Payload\\\": {\\n        \\\"usage\\\": {\\n          \\\"text_words\\\": 2\\n        }\\n      }\\n    }\\n  ]\\n}\",\"inputLanguage\":\"\",\"outputLanguage\":\"\"}]}"} />);
```
