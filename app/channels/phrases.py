"""The fixed sentences a channel says, in English, Hindi and Tamil, kept in one place.

For any other language, a sentence said to a linked person is translated by the small model at no credit cost (see `tell`), and falls back to
English when that fails. A sentence said to someone who is not linked is never sent through a model: it is one of these, or English."""
from __future__ import annotations

from typing import Any, Optional

PHRASES: dict[str, dict[str, str]] = {
    "hint": {
        "en": "Hi! To use Ekavo here, open Settings in the Ekavo app, choose Chat apps, and send me the code it shows. It looks like: LINK ABCD-2345",
        "hi": "नमस्ते! यहाँ Ekavo इस्तेमाल करने के लिए Ekavo ऐप में Settings खोलें, Chat apps चुनें और वहाँ दिखा कोड मुझे भेजें। जैसे: LINK ABCD-2345",
        "ta": "வணக்கம்! இங்கே Ekavo பயன்படுத்த, Ekavo செயலியில் Settings திறந்து, Chat apps தேர்ந்தெடுத்து, அங்கே தெரியும் குறியீட்டை எனக்கு அனுப்புங்கள். உதாரணம்: LINK ABCD-2345",
    },
    "linked": {
        "en": "You are linked to {workspace} on Ekavo.\n\nSend me an idea and I will turn it into a draft:\n• a line of text\n• a link to a page or a video\n• a voice note, in any language\n\nNothing is posted without your yes. Every draft opens in Ekavo, so you can read it and change it first.\n\nHandy words: NEW starts a fresh chat. REMIND ON gives you one nudge a day. STOP unlinks this chat.",
        "hi": "आप Ekavo पर {workspace} से जुड़ गए हैं।\n\nमुझे कोई विचार भेजिए, मैं उसे ड्राफ्ट बना दूँगा:\n• एक पंक्ति का संदेश\n• किसी पेज या वीडियो का लिंक\n• किसी भी भाषा में वॉइस नोट\n\nआपकी हाँ के बिना कुछ भी पोस्ट नहीं होता। हर ड्राफ्ट Ekavo में खुलता है, ताकि आप उसे पढ़कर बदल सकें।\n\nकाम के शब्द: NEW नई बातचीत शुरू करता है। REMIND ON दिन में एक बार याद दिलाता है। STOP इस चैट का लिंक हटाता है।",
        "ta": "நீங்கள் Ekavo-வில் {workspace} உடன் இணைக்கப்பட்டுள்ளீர்கள்.\n\nஒரு யோசனையை அனுப்புங்கள், நான் அதை வரைவாக மாற்றுகிறேன்:\n• ஒரு வரி செய்தி\n• ஒரு பக்கம் அல்லது வீடியோவின் இணைப்பு\n• எந்த மொழியிலும் குரல் பதிவு\n\nஉங்கள் சம்மதம் இல்லாமல் எதுவும் பதிவிடப்படாது. ஒவ்வொரு வரைவும் Ekavo-வில் திறக்கும், படித்து மாற்றலாம்.\n\nபயனுள்ள சொற்கள்: NEW புதிய உரையாடலைத் தொடங்கும். REMIND ON நாளுக்கு ஒரு நினைவூட்டல் தரும். STOP இந்த அரட்டையின் இணைப்பை நீக்கும்.",
    },
    "your_workspace": {"en": "your workspace", "hi": "आपका वर्कस्पेस", "ta": "உங்கள் வொர்க்ஸ்பேஸ்"},
    "link_bad": {
        "en": "That code did not work. It may have expired or been used. Make a new one in the app.",
        "hi": "यह कोड काम नहीं किया। यह पुराना हो गया होगा या इस्तेमाल हो चुका होगा। ऐप में नया बनाएँ।",
        "ta": "அந்தக் குறியீடு வேலை செய்யவில்லை. அது காலாவதியாகி இருக்கலாம் அல்லது ஏற்கனவே பயன்படுத்தப்பட்டிருக்கலாம். செயலியில் புதியதை உருவாக்குங்கள்.",
    },
    "stopped": {
        "en": "Unlinked. I will not message you again. Link again any time from the app.",
        "hi": "लिंक हटा दिया। मैं अब संदेश नहीं भेजूँगा। ऐप से कभी भी दोबारा जोड़ सकते हैं।",
        "ta": "இணைப்பு நீக்கப்பட்டது. இனி செய்தி அனுப்ப மாட்டேன். செயலியில் எப்போது வேண்டுமானாலும் மீண்டும் இணைக்கலாம்.",
    },
    "slow_down": {
        "en": "That is a lot of messages in a short time. Please wait a little and try again.",
        "hi": "बहुत कम समय में बहुत सारे संदेश आ गए। थोड़ा रुककर फिर कोशिश करें।",
        "ta": "குறுகிய நேரத்தில் நிறைய செய்திகள். சிறிது நேரம் காத்திருந்து மீண்டும் முயலுங்கள்.",
    },
    "voice_failed": {
        "en": "I could not hear that voice note. Please try again, or write it as a message.",
        "hi": "मैं यह वॉइस नोट समझ नहीं पाया। कृपया फिर भेजें या संदेश लिखकर भेजें।",
        "ta": "அந்தக் குரல் பதிவைப் புரிந்துகொள்ள முடியவில்லை. மீண்டும் அனுப்புங்கள் அல்லது செய்தியாக எழுதுங்கள்.",
    },
    "pictures_soon": {
        "en": "I can not use pictures yet. Send your idea as a message or a voice note.",
        "hi": "मैं अभी तस्वीरों का उपयोग नहीं कर सकता। अपना विचार संदेश या वॉइस नोट में भेजें।",
        "ta": "இப்போது படங்களைப் பயன்படுத்த முடியாது. உங்கள் யோசனையை செய்தியாகவோ குரல் பதிவாகவோ அனுப்புங்கள்.",
    },
    "nothing_waiting": {
        "en": "There is nothing waiting for your yes.",
        "hi": "आपकी हाँ के लिए कुछ भी इंतज़ार में नहीं है।",
        "ta": "உங்கள் சம்மதத்திற்காக எதுவும் காத்திருக்கவில்லை.",
    },
    "cancelled": {
        "en": "Cancelled. Nothing was done or charged.",
        "hi": "रद्द किया। कुछ नहीं किया गया और कोई शुल्क नहीं लगा।",
        "ta": "ரத்து செய்யப்பட்டது. எதுவும் செய்யப்படவில்லை, கட்டணமும் இல்லை.",
    },
    "off": {
        "en": "This chat channel is paused for now. Use the Ekavo app meanwhile.",
        "hi": "यह चैट सेवा अभी रुकी हुई है। तब तक Ekavo ऐप का उपयोग करें।",
        "ta": "இந்த அரட்டை சேவை இப்போது நிறுத்தப்பட்டுள்ளது. அதுவரை Ekavo செயலியைப் பயன்படுத்துங்கள்.",
    },
    "done": {
        "en": "Done. Open it to look, edit and approve:",
        "hi": "हो गया। देखने, बदलने और मंज़ूर करने के लिए खोलें:",
        "ta": "முடிந்தது. பார்க்க, திருத்த, அனுமதிக்க திறக்கவும்:",
    },
    "failed": {
        "en": "That did not work. Nothing more was charged. Try again in a minute.",
        "hi": "यह नहीं हो पाया। आगे कोई शुल्क नहीं लगा। एक मिनट बाद फिर कोशिश करें।",
        "ta": "இது நடக்கவில்லை. மேலும் கட்டணம் இல்லை. ஒரு நிமிடத்தில் மீண்டும் முயலுங்கள்.",
    },
    "partial": {
        "en": "Some of this finished and some did not. Open the app to see what was made.",
        "hi": "इसका कुछ हिस्सा पूरा हुआ और कुछ नहीं। क्या बना है, देखने के लिए ऐप खोलें।",
        "ta": "இதில் சில முடிந்தன, சில முடியவில்லை. என்ன உருவானது என்று பார்க்க செயலியைத் திறக்கவும்.",
    },
    "found": {"en": "Here is what I found:", "hi": "यह मिला:", "ta": "கிடைத்தது:"},
    "fresh": {
        "en": "Started a new conversation. What would you like to do?",
        "hi": "नई बातचीत शुरू की। आप क्या करना चाहेंगे?",
        "ta": "புதிய உரையாடல் தொடங்கியது. என்ன செய்ய வேண்டும்?",
    },
    "ref_saved": {
        "en": "Got the picture. Now tell me what to make and I will follow its look.",
        "hi": "तस्वीर मिल गई। अब बताइए क्या बनाना है, मैं इसी का लुक अपनाऊँगा।",
        "ta": "படம் கிடைத்தது. இப்போது என்ன செய்ய வேண்டும் என்று சொல்லுங்கள், இதன் தோற்றத்தைப் பின்பற்றுகிறேன்.",
    },
    "ref_bad": {
        "en": "I could not read that picture. Try a JPG or PNG.",
        "hi": "मैं यह तस्वीर पढ़ नहीं पाया। JPG या PNG भेजें।",
        "ta": "அந்தப் படத்தைப் படிக்க முடியவில்லை. JPG அல்லது PNG அனுப்புங்கள்.",
    },
    "remind_on": {
        "en": "Reminders are on. At most one message a day, never at night, and only to tell you something is waiting. Send REMIND OFF to stop them.",
        "hi": "रिमाइंडर चालू हैं। दिन में अधिकतम एक संदेश, रात में कभी नहीं, और सिर्फ़ यह बताने के लिए कि कुछ इंतज़ार में है। बंद करने के लिए REMIND OFF भेजें।",
        "ta": "நினைவூட்டல்கள் இயக்கத்தில் உள்ளன. நாளுக்கு ஒரு செய்தி மட்டும், இரவில் இல்லை, ஏதாவது காத்திருந்தால் மட்டும். நிறுத்த REMIND OFF அனுப்புங்கள்.",
    },
    "remind_off": {
        "en": "Reminders are off, and your chat id has been removed.",
        "hi": "रिमाइंडर बंद हैं, और आपकी चैट आईडी हटा दी गई है।",
        "ta": "நினைவூட்டல்கள் நிறுத்தப்பட்டன, உங்கள் அரட்டை அடையாளம் நீக்கப்பட்டது.",
    },
    "remind_unavailable": {
        "en": "Reminders are not available right now. You can still ask me anything here.",
        "hi": "रिमाइंडर अभी उपलब्ध नहीं हैं। आप यहाँ फिर भी कुछ भी पूछ सकते हैं।",
        "ta": "நினைவூட்டல்கள் இப்போது கிடைக்கவில்லை. இங்கே எதையும் கேட்கலாம்.",
    },
    "still_working": {
        "en": "Still working on it. I will message you here the moment it is ready.",
        "hi": "अभी भी काम चल रहा है। तैयार होते ही मैं यहीं संदेश भेजूँगा।",
        "ta": "இன்னும் வேலை நடக்கிறது. தயாரானவுடன் இங்கே செய்தி அனுப்புவேன்.",
    },
    "go": {"en": "Go ahead", "hi": "आगे बढ़ें", "ta": "தொடரவும்"},
    "cancel": {"en": "Cancel", "hi": "रद्द करें", "ta": "ரத்து"},
}


def known(key: str, language: Optional[str]) -> str:
    """The sentence in the language when it is one of the three written here, else the English one."""
    table = PHRASES[key]
    return table.get((language or "en")[:2].lower(), table["en"])


async def tell(key: str, language: Optional[str], *, localize: bool = False, think: Any = None) -> str:
    """A fixed sentence. For another language, translated by the small model when `localize` is set (only ever for a linked person)."""
    code = (language or "en")[:2].lower()
    table = PHRASES[key]
    if code in table or not localize:
        return table.get(code, table["en"])
    from app.ask import runs

    return await runs.localize(table["en"], code, think=think)


async def linked_text(language: Optional[str], workspace: Optional[str]) -> str:
    """The welcome said right after a chat is linked: the workspace by name, what can be sent, and the three handy words. In English, Hindi or Tamil it
    is the written text; in another language it is translated once and remembered, and the English is sent when that is not ready in time."""
    code = (language or "en")[:2].lower()
    name = (workspace or "").strip() or PHRASES["your_workspace"].get(code, PHRASES["your_workspace"]["en"])
    table = PHRASES["linked"]
    if code in table:
        return table[code].replace("{workspace}", name)
    from app.shared.reader_language import say

    return await say("channel.linked", table["en"].replace("{workspace}", "{name}"), language, {"name": name})
