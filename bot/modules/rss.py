from asyncio import sleep
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta
from typing import List, Tuple, Optional
import re
from math import ceil

from httpx import AsyncClient, Limits
from bs4 import BeautifulSoup as bs
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.filters import command
from pyrogram import filters
from pyrogram.errors import WebpageCurlFailed, WebpageMediaEmpty, FloodWait, ChatWriteForbidden, UserIsBlocked, PeerIdInvalid

from bot import bot, LOGGER, DATABASE_URL, scheduler
from bot.helper.telegram_helper.message_utils import send_message, edit_message, delete_message
from bot.helper.telegram_helper.filters import CustomFilters
from bot.helper.telegram_helper.button_build import ButtonMaker

FAILED_PIC = "https://telegra.ph/file/09733b49f3a9d5b147d21.png"
URL_LIVECHART_NEWS = 'https://www.livechart.me/feeds/headlines'
RSS_DELAY = 300
REQUEST_TIMEOUT = 30.0
MAX_FEED_TITLE_LENGTH = 50
FEEDS_PER_PAGE = 5
GROUPS_PER_PAGE = 5
TIMEZONE_OFFSET = timedelta(hours=6, minutes=30)
MAX_CONSECUTIVE_FAILURES = 3
http_client = None

if DATABASE_URL:
    try:
        from bot import get_collection
        
        RSS_SETTINGS = get_collection('rss_settings')
        RSS_FEEDS = get_collection('rss_feeds')
        RSS_FEED_DATA = get_collection('rss_feed_data')
        LIVECHARTME_SETTINGS = get_collection('livechartme_settings')
        LIVECHARTME_DATA = get_collection('livechartme_data')
        LIVECHARTME_GROUPS = get_collection('livechartme_groups')
        
        http_client = AsyncClient(
            timeout=REQUEST_TIMEOUT,
            follow_redirects=True,
            limits=Limits(max_keepalive_connections=5, max_connections=10)
        )
        
        LOGGER.info(f"RSS Manager started (checking every {RSS_DELAY}s)")
    except Exception as e:
        LOGGER.error(f"RSS Manager: Initialization failed: {e}")
        DATABASE_URL = None
else:
    LOGGER.warning("RSS Manager: MongoDB not configured")


def format_time(dt: Optional[datetime]) -> str:
    if not dt:
        return "Never"
    yangon_time = dt + TIMEZONE_OFFSET
    return yangon_time.strftime('%Y-%m-%d | %I:%M %p')


async def get_rss_settings(user_id: int) -> dict:
    settings = await RSS_SETTINGS.find_one({"_id": user_id})
    if not settings:
        settings = {"_id": user_id, "rss_enabled": False, "feeds_enabled": False}
        await RSS_SETTINGS.insert_one(settings)
    return settings


async def get_livechartme_settings(user_id: int) -> dict:
    settings = await LIVECHARTME_SETTINGS.find_one({"_id": user_id})
    if not settings:
        settings = {"_id": user_id, "enabled": False}
        await LIVECHARTME_SETTINGS.insert_one(settings)
    return settings


async def update_rss_settings(user_id: int, rss_enabled: bool = None, feeds_enabled: bool = None):
    update_dict = {}
    if rss_enabled is not None:
        update_dict["rss_enabled"] = rss_enabled
    if feeds_enabled is not None:
        update_dict["feeds_enabled"] = feeds_enabled
    
    if update_dict:
        await RSS_SETTINGS.update_one(
            {"_id": user_id},
            {"$set": update_dict},
            upsert=True
        )


async def update_livechartme_settings(user_id: int, enabled: bool):
    await LIVECHARTME_SETTINGS.update_one(
        {"_id": user_id},
        {"$set": {"enabled": enabled, "updated_at": datetime.utcnow()}},
        upsert=True
    )


async def get_user_feeds(user_id: int) -> List[dict]:
    feeds = []
    async for feed in RSS_FEEDS.find({"user_id": user_id}).sort("title", 1):
        feeds.append(feed)
    return feeds


async def get_user_groups(user_id: int) -> List[dict]:
    groups = []
    async for group in LIVECHARTME_GROUPS.find({"user_id": user_id}).sort("added_at", -1):
        groups.append(group)
    return groups


async def add_livechart_group(user_id: int, group_id: int, group_title: str = None) -> Tuple[bool, str]:
    existing = await LIVECHARTME_GROUPS.find_one({"user_id": user_id, "group_id": group_id})
    if existing:
        return False, "This group is already added!"
    
    try:
        test_msg = await bot.send_message(group_id, "Testing LiveChart.me group configuration...")
        await test_msg.delete()
        
        if not group_title:
            try:
                chat = await bot.get_chat(group_id)
                group_title = chat.title or f"Group {group_id}"
            except:
                group_title = f"Group {group_id}"
        
    except (ChatWriteForbidden, UserIsBlocked):
        return False, "Bot doesn't have permission to send messages in this group!"
    except PeerIdInvalid:
        return False, "Invalid group ID! Make sure the bot is added to the group."
    except Exception as e:
        LOGGER.error(f"Error validating group {group_id}: {e}")
        return False, f"Failed to validate group: {str(e)[:100]}"
    
    group_doc = {
        "_id": f"lcgroup_{user_id}_{int(datetime.utcnow().timestamp())}",
        "user_id": user_id,
        "group_id": group_id,
        "group_title": group_title[:100],
        "added_at": datetime.utcnow(),
        "last_message": None
    }
    
    await LIVECHARTME_GROUPS.insert_one(group_doc)
    LOGGER.info(f"Added LiveChart group {group_id} for user {user_id}")
    return True, f"Successfully added group **{group_title}**"


async def remove_livechart_group(user_id: int, group_doc_id: str) -> Tuple[bool, str]:
    result = await LIVECHARTME_GROUPS.delete_one({"_id": group_doc_id, "user_id": user_id})
    if result.deleted_count > 0:
        LOGGER.info(f"Removed LiveChart group {group_doc_id} for user {user_id}")
        return True, "Group removed successfully!"
    return False, "Group not found!"


async def add_feed(user_id: int, url: str, title: str) -> Tuple[bool, str]:
    existing = await RSS_FEEDS.find_one({"user_id": user_id, "url": url})
    if existing:
        return False, "This feed URL is already subscribed!"
    
    try:
        LOGGER.info(f"Validating feed: {url}")
        response = await http_client.get(url)
        response.raise_for_status()
        
        soup = bs(response.text, features='xml')
        items = soup.find_all('item')
        entries = soup.find_all('entry')
        
        if not items and not entries:
            LOGGER.warning(f"No items found in feed: {url}")
            return False, "Not a valid RSS/Atom feed - no items found"
        
        first_item = items[0] if items else entries[0]
        
        if items:
            guid_tag = first_item.find('guid')
            link_tag = first_item.find('link')
            last_guid = guid_tag.get_text(strip=True) if guid_tag else (
                link_tag.get_text(strip=True) if link_tag else ""
            )
        else:
            id_tag = first_item.find('id')
            last_guid = id_tag.get_text(strip=True) if id_tag else ""
        
        item_count = len(items) if items else len(entries)
        
        etag = response.headers.get('ETag')
        last_modified = response.headers.get('Last-Modified')
        
        LOGGER.info(f"Feed validated: {url} ({item_count} items)")
            
    except Exception as e:
        error_msg = str(e)
        LOGGER.error(f"Feed validation failed for {url}: {error_msg}")
        
        if "timeout" in error_msg.lower() or "timed out" in error_msg.lower():
            return False, "Feed took too long to respond - check URL or try again later"
        elif "connection" in error_msg.lower() or "connect" in error_msg.lower():
            return False, "Cannot reach server - check if URL is correct"
        elif hasattr(e, 'response') and hasattr(e.response, 'status_code'):
            return False, f"Server error ({e.response.status_code}) - feed may be unavailable"
        else:
            return False, f"Feed validation failed: {error_msg[:100]}"
    
    feed_id = f"feed_{user_id}_{int(datetime.utcnow().timestamp())}"
    feed_doc = {
        "_id": feed_id,
        "user_id": user_id,
        "url": url,
        "title": title[:MAX_FEED_TITLE_LENGTH],
        "enabled": True,
        "created_at": datetime.utcnow()
    }
    
    await RSS_FEEDS.insert_one(feed_doc)
    
    await RSS_FEED_DATA.insert_one({
        "feed_id": feed_id,
        "last_guid": last_guid,
        "last_checked": None,
        "etag": etag,
        "last_modified": last_modified,
        "total_items": 0,
        "check_count": 0,
        "success_count": 0,
        "consecutive_failures": 0,
        "created_at": datetime.utcnow()
    })
    
    LOGGER.info(f"Added feed '{title}' for user {user_id}")
    return True, f"Successfully subscribed to **{title}**\n\nFound {item_count} items in feed"


async def remove_feed(user_id: int, feed_id: str) -> Tuple[bool, str]:
    result = await RSS_FEEDS.delete_one({"_id": feed_id, "user_id": user_id})
    if result.deleted_count > 0:
        await RSS_FEED_DATA.delete_one({"feed_id": feed_id})
        LOGGER.info(f"Removed feed {feed_id} for user {user_id}")
        return True, "Feed removed successfully!"
    return False, "Feed not found!"


async def toggle_feed(user_id: int, feed_id: str) -> Tuple[bool, str, bool]:
    feed = await RSS_FEEDS.find_one({"_id": feed_id, "user_id": user_id})
    if not feed:
        return False, "Feed not found!", False
    
    new_status = not feed.get("enabled", True)
    await RSS_FEEDS.update_one(
        {"_id": feed_id},
        {"$set": {"enabled": new_status}}
    )
    
    if new_status:
        await RSS_FEED_DATA.update_one(
            {"feed_id": feed_id},
            {"$set": {"consecutive_failures": 0}}
        )
    
    status_text = "enabled" if new_status else "disabled"
    LOGGER.info(f"Feed {feed_id} {status_text} by user {user_id}")
    return True, f"Feed **{feed['title']}** {status_text}!", new_status


async def send_rss_message(chat_id: int, item_title: str, item_link: str, feed_name: str, image_url: str = None):
    try:
        buttons = ButtonMaker()
        buttons.url_button("Read More", item_link)
        
        caption = f"**{item_title}**\n\nFrom: {feed_name}\n\n#RSS"
        
        if image_url:
            try:
                msg = await bot.send_photo(
                    chat_id=chat_id,
                    photo=image_url,
                    caption=caption,
                    reply_markup=buttons.build_menu(1),
                    disable_notification=True
                )
                return msg
            except (WebpageCurlFailed, WebpageMediaEmpty):
                LOGGER.warning(f"Image failed for {feed_name}, using fallback")
                try:
                    msg = await bot.send_photo(
                        chat_id=chat_id,
                        photo=FAILED_PIC,
                        caption=caption,
                        reply_markup=buttons.build_menu(1),
                        disable_notification=True
                    )
                    return msg
                except:
                    pass
        
        msg = await bot.send_message(
            chat_id=chat_id,
            text=caption,
            reply_markup=buttons.build_menu(1),
            disable_notification=True
        )
        return msg
        
    except FloodWait as e:
        LOGGER.warning(f"FloodWait {e.value}s for user {chat_id}")
        await sleep(e.value)
        return await send_rss_message(chat_id, item_title, item_link, feed_name, image_url)
    except Exception as e:
        LOGGER.error(f"Error sending RSS message to {chat_id}: {e}")
        return None


def extract_text_from_tag(tag) -> str:
    if tag is None:
        return ""
    return tag.get_text(strip=True)


async def fetch_livechart_news():
    try:
        response = await http_client.get(URL_LIVECHART_NEWS)
        response.raise_for_status()
        return bs(response.text, features='xml')
    except Exception as e:
        LOGGER.error(f"LiveChart fetch error: {e}")
        return None


async def send_livechart_to_target(chat_id: int, item: dict):
    try:
        buttons = ButtonMaker()
        
        if item['link']:
            buttons.url_button("More Info", item['source'])
        
        if item['source']:
            buttons.url_button("Source", item['link'])
        
        caption = f"**{item['title']}**\n\n#LiveChartMe"
        
        if item['image']:
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=item['image'],
                    caption=caption,
                    reply_markup=buttons.build_menu(2),
                    disable_notification=True
                )
                return True
            except (WebpageCurlFailed, WebpageMediaEmpty):
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=FAILED_PIC,
                    caption=caption,
                    reply_markup=buttons.build_menu(2),
                    disable_notification=True
                )
                return True
        else:
            await bot.send_message(
                chat_id=chat_id,
                text=caption,
                reply_markup=buttons.build_menu(2),
                disable_notification=True
            )
            return True
    except (ChatWriteForbidden, UserIsBlocked):
        LOGGER.warning(f"Cannot send to {chat_id} - forbidden or blocked")
        return False
    except Exception as e:
        LOGGER.error(f"Error sending LiveChart message to {chat_id}: {e}")
        return False


async def process_livechart():
    try:
        subscribers = []
        async for user in LIVECHARTME_SETTINGS.find({"enabled": True}):
            subscribers.append(user['_id'])
        
        all_groups = []
        async for group in LIVECHARTME_GROUPS.find():
            all_groups.append(group)
        
        if not subscribers and not all_groups:
            return
        
        LOGGER.info("Checking LiveChart.me for updates...")
        soup = await fetch_livechart_news()
        if not soup:
            LOGGER.warning("Failed to fetch LiveChart.me feed")
            return
        
        first_item = soup.find('item')
        if not first_item:
            LOGGER.warning("No items in LiveChart.me feed")
            return
        
        first_guid_tag = first_item.find('guid')
        first_guid = first_guid_tag.get_text(strip=True) if first_guid_tag else ""
        
        if not first_guid:
            LOGGER.warning("No GUID found in first LiveChart item")
            return
        
        stored = await LIVECHARTME_DATA.find_one({'_id': 'latest'})
        
        if not stored:
            await LIVECHARTME_DATA.insert_one({
                '_id': 'latest',
                'guid': first_guid,
                'updated_at': datetime.utcnow()
            })
            LOGGER.info(f"LiveChartMe initialized")
            return
        
        if stored.get('guid') == first_guid:
            return
        
        new_items = []
        for item in soup.find_all("item"):
            guid_tag = item.find('guid')
            guid_str = guid_tag.get_text(strip=True) if guid_tag else ""
            
            if guid_str == stored.get('guid'):
                break
            
            title_tag = item.find('title')
            link_tag = item.find('link')
            source_tag = item.find('guid')
            
            title = title_tag.get_text(strip=True) if title_tag else ""
            link = link_tag.get_text(strip=True) if link_tag else ""
            source_url = source_tag.get_text(strip=True) if source_tag else ""
            
            enclosure = item.find('enclosure')
            img_url = None
            if enclosure and enclosure.get('url'):
                img_url = str(enclosure.get('url')).split('?')[0]
            
            description = item.find('description')
            if description:
                desc_html = str(description)
            
            new_items.append({
                'title': title,
                'link': link,
                'source': source_url,
                'image': img_url
            })
        
        if not new_items:
            return
        
        for chat_id in subscribers:
            for item in reversed(new_items):
                await send_livechart_to_target(chat_id, item)
                await sleep(1.5)
        
        for group_doc in all_groups:
            for item in reversed(new_items):
                success = await send_livechart_to_target(group_doc['group_id'], item)
                if success:
                    await LIVECHARTME_GROUPS.update_one(
                        {"_id": group_doc['_id']},
                        {"$set": {"last_message": datetime.utcnow()}}
                    )
                await sleep(1.5)
        
        await LIVECHARTME_DATA.update_one(
            {'_id': 'latest'},
            {'$set': {'guid': first_guid, 'updated_at': datetime.utcnow()}},
            upsert=True
        )
        
        total_targets = len(subscribers) + len(all_groups)
        LOGGER.info(f"Sent {len(new_items)} LiveChart headlines to {total_targets} targets")
            
    except Exception as e:
        LOGGER.error(f"LiveChartMe processing error: {e}", exc_info=True)


async def fetch_rss_feed(url: str, etag: str = None, last_modified: str = None, force_fetch: bool = False):
    try:
        headers = {}
        
        if not force_fetch:
            if etag:
                headers['If-None-Match'] = etag
            if last_modified:
                headers['If-Modified-Since'] = last_modified
        
        response = await http_client.get(url, headers=headers)
        
        if response.status_code == 304 and not force_fetch:
            return None, None, None
        
        response.raise_for_status()
        
        new_etag = response.headers.get('ETag')
        new_last_modified = response.headers.get('Last-Modified')
        
        return bs(response.text, features='xml'), new_etag, new_last_modified
        
    except Exception as e:
        LOGGER.error(f"Error fetching {url}: {e}")
        return False, None, None


async def process_rss_feed(feed: dict, is_manual: bool = False):
    try:
        if not feed.get("enabled", True) and not is_manual:
            return None
        
        LOGGER.info(f"Checking feed: {feed['title']}")
        
        feed_data = await RSS_FEED_DATA.find_one({"feed_id": feed['_id']})
        
        etag = feed_data.get('etag') if feed_data else None
        last_modified = feed_data.get('last_modified') if feed_data else None
        
        soup, new_etag, new_last_modified = await fetch_rss_feed(
            feed['url'], etag, last_modified, force_fetch=is_manual
        )
        
        if soup is False:
            LOGGER.warning(f"Failed to fetch {feed['title']}")
            
            if feed_data and not is_manual:
                consecutive_failures = feed_data.get('consecutive_failures', 0) + 1
                
                await RSS_FEED_DATA.update_one(
                    {"feed_id": feed['_id']},
                    {
                        "$set": {
                            "last_checked": datetime.utcnow(),
                            "consecutive_failures": consecutive_failures
                        },
                        "$inc": {"check_count": 1}
                    }
                )
                
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    await RSS_FEEDS.update_one(
                        {"_id": feed['_id']},
                        {"$set": {"enabled": False}}
                    )
                    LOGGER.warning(f"Feed {feed['title']} auto-disabled")
                    
                    try:
                        await bot.send_message(
                            feed['user_id'],
                            f"**Feed Auto-Disabled**\n\n"
                            f"Feed **{feed['title']}** has failed {consecutive_failures} times.\n\n"
                            f"Please check if the feed URL is still valid."
                        )
                    except:
                        pass
            
            return None
        
        if soup is None:
            if feed_data and not is_manual:
                await RSS_FEED_DATA.update_one(
                    {"feed_id": feed['_id']},
                    {
                        "$set": {
                            "last_checked": datetime.utcnow(),
                            "consecutive_failures": 0
                        },
                        "$inc": {"check_count": 1, "success_count": 1}
                    }
                )
            return None
        
        if not feed_data:
            first_item = soup.find('item') or soup.find('entry')
            
            if first_item:
                if soup.find('item'):
                    guid_tag = first_item.find('guid')
                    link_tag = first_item.find('link')
                    last_guid = guid_tag.get_text(strip=True) if guid_tag else (
                        link_tag.get_text(strip=True) if link_tag else ""
                    )
                else:
                    id_tag = first_item.find('id')
                    last_guid = id_tag.get_text(strip=True) if id_tag else ""
                
                current_time = datetime.utcnow()
                await RSS_FEED_DATA.insert_one({
                    "feed_id": feed['_id'],
                    "last_guid": last_guid,
                    "last_checked": current_time,
                    "etag": new_etag,
                    "last_modified": new_last_modified,
                    "total_items": 0,
                    "check_count": 1,
                    "success_count": 1,
                    "consecutive_failures": 0,
                    "created_at": current_time
                })
                LOGGER.info(f"Initialized {feed['title']}")
            return None
        
        last_guid = feed_data.get('last_guid', '') if not is_manual else ''
        
        new_items = []
        
        for item in soup.find_all("item")[:15]:
            guid_tag = item.find('guid')
            link_tag = item.find('link')
            
            item_guid = guid_tag.get_text(strip=True) if guid_tag else (
                link_tag.get_text(strip=True) if link_tag else ""
            )
            
            if not is_manual and item_guid and item_guid == last_guid:
                break
            
            title = extract_text_from_tag(item.find('title'))
            link = extract_text_from_tag(item.find('link'))
            
            image = None
            enclosure = item.find('enclosure')
            if enclosure:
                enc_type = str(enclosure.get('type', ''))
                if 'image' in enc_type or 'jpg' in enc_type or 'png' in enc_type:
                    image = enclosure.get('url')
            
            if not image:
                media = item.find('media:content') or item.find('media:thumbnail')
                if media:
                    image = media.get('url')
            
            new_items.append({
                'title': title,
                'link': link,
                'image': image,
                'guid': item_guid
            })
            
            if is_manual:
                break
        
        if not new_items:
            for entry in soup.find_all("entry")[:15]:
                id_tag = entry.find('id')
                entry_id = id_tag.get_text(strip=True) if id_tag else ""
                
                if not is_manual and entry_id and entry_id == last_guid:
                    break
                
                title = extract_text_from_tag(entry.find('title'))
                link_tag = entry.find('link')
                link = link_tag.get('href') if link_tag and link_tag.get('href') else ""
                
                new_items.append({
                    'title': title,
                    'link': link,
                    'image': None,
                    'guid': entry_id
                })
                
                if is_manual:
                    break
        
        if not new_items:
            if not is_manual:
                await RSS_FEED_DATA.update_one(
                    {"feed_id": feed['_id']},
                    {
                        "$set": {
                            "last_checked": datetime.utcnow(),
                            "etag": new_etag,
                            "last_modified": new_last_modified,
                            "consecutive_failures": 0
                        },
                        "$inc": {"check_count": 1, "success_count": 1}
                    }
                )
            return None
        
        LOGGER.info(f"Found {len(new_items)} new items in {feed['title']}")
        
        if is_manual:
            return new_items[0]
        
        chat_id = feed['user_id']
        
        for item in reversed(new_items):
            await send_rss_message(
                chat_id,
                item['title'],
                item['link'],
                feed['title'],
                item.get('image')
            )
            await sleep(1.5)
        
        first_new = new_items[0]
        await RSS_FEED_DATA.update_one(
            {"feed_id": feed['_id']},
            {
                "$set": {
                    "last_guid": first_new['guid'],
                    "last_checked": datetime.utcnow(),
                    "etag": new_etag,
                    "last_modified": new_last_modified,
                    "consecutive_failures": 0
                },
                "$inc": {
                    "total_items": len(new_items),
                    "check_count": 1,
                    "success_count": 1
                }
            }
        )
        
        LOGGER.info(f"Sent {len(new_items)} items from {feed['title']}")
        return True
        
    except Exception as e:
        LOGGER.error(f"Error processing feed: {e}", exc_info=True)
        
        if not is_manual:
            feed_data = await RSS_FEED_DATA.find_one({"feed_id": feed['_id']})
            if feed_data:
                await RSS_FEED_DATA.update_one(
                    {"feed_id": feed['_id']},
                    {
                        "$set": {"last_checked": datetime.utcnow()},
                        "$inc": {
                            "check_count": 1,
                            "consecutive_failures": 1
                        }
                    }
                )
        return None


async def rss_monitor():
    if not DATABASE_URL:
        LOGGER.warning("DATABASE_URL not configured! Shutting down rss scheduler...")
        scheduler.shutdown(wait=False)
        return
    
    try:
        LOGGER.info("=" * 50)
        LOGGER.info("RSS Monitor: Starting check cycle...")
        
        rss_enabled_users = []
        async for settings in RSS_SETTINGS.find({"rss_enabled": True}):
            rss_enabled_users.append(settings['_id'])
        
        if not rss_enabled_users:
            LOGGER.info("RSS master switch disabled for all users, skipping cycle")
            return
        
        lc_users = []
        async for user in LIVECHARTME_SETTINGS.find({"enabled": True}):
            if user['_id'] in rss_enabled_users:
                lc_users.append(user['_id'])
        
        has_groups = await LIVECHARTME_GROUPS.count_documents({}) > 0
        
        if lc_users or has_groups:
            await process_livechart()
        
        feed_users = []
        async for settings in RSS_SETTINGS.find({"rss_enabled": True, "feeds_enabled": True}):
            feed_users.append(settings['_id'])
        
        if feed_users:
            LOGGER.info(f"Processing feeds for {len(feed_users)} user(s)")
            
            for user_id in feed_users:
                user_feeds = []
                async for feed in RSS_FEEDS.find({"user_id": user_id, "enabled": True}):
                    user_feeds.append(feed)
                
                if user_feeds:
                    for feed in user_feeds:
                        await process_rss_feed(feed)
                        await sleep(2)
        
        LOGGER.info("RSS Monitor: Check cycle completed")
        LOGGER.info("=" * 50)
        
    except Exception as e:
        LOGGER.error(f"RSS Monitor error: {e}", exc_info=True)


async def rss_menu(client, message):
    if not DATABASE_URL:
        await send_message(
            message,
            "RSS Manager requires MongoDB to be configured."
        )
        return
    
    user_id = message.from_user.id
    rss_settings = await get_rss_settings(user_id)
    lc_settings = await get_livechartme_settings(user_id)
    feeds = await get_user_feeds(user_id)
    groups = await get_user_groups(user_id)
    
    buttons = ButtonMaker()
    
    feeds_status = "✅ Enabled" if rss_settings.get("feeds_enabled") else "❌ Disabled"
    buttons.data_button(f"Feeds: {feeds_status}", "rss_toggle_feeds")
    
    lc_status = "✅ Enabled" if lc_settings.get("enabled") else "❌ Disabled"
    buttons.data_button(f"LiveChartMe: {lc_status}", "rss_toggle_livechart")
    
    rss_status = "✅ Enabled" if rss_settings.get("rss_enabled") else "❌ Disabled"
    buttons.data_button(f"RSS: {rss_status}", "rss_toggle_main")
    
    buttons.data_button("LiveChartMe Groups", "rss_lc_groups_menu")
    buttons.data_button(f"My Feeds ({len(feeds)})", "rss_list 0")
    buttons.data_button("Add Feed", "rss_add")
    buttons.data_button("Close", "rss_close")
    
    text = (
        "**RSS Feed Manager**\n\n"
        "Manage your RSS feed subscriptions and get real-time updates.\n\n"
        f"**RSS Service:** {rss_status}\n"
        f"**Feeds Delivery:** {feeds_status}\n"
        f"**LiveChartMe Headlines:** {lc_status}\n"
        f"**Active Feeds:** {len(feeds)}\n"
        f"**LiveChart Groups:** {len(groups)}\n\n"
        "Enable RSS to start the monitoring service. "
        "Enable Feeds/LiveChartMe to receive updates."
    )
    
    await send_message(message, text, buttons.build_menu(2))


async def rss_callback(client, query):
    data = query.data
    user_id = query.from_user.id
    message = query.message
    
    try:
        if data == "rss_close":
            await query.answer()
            if message.reply_to_message:
                try:
                    await message.reply_to_message.delete()
                except:
                    pass
            await message.delete()
            return
        
        elif data == "rss_toggle_main":
            settings = await get_rss_settings(user_id)
            new_status = not settings.get("rss_enabled", False)
            await update_rss_settings(user_id, rss_enabled=new_status)
            
            await query.answer(f"RSS {'enabled' if new_status else 'disabled'}!")
            await refresh_main_menu(message, user_id)
        
        elif data == "rss_toggle_feeds":
            settings = await get_rss_settings(user_id)
            new_status = not settings.get("feeds_enabled", False)
            await update_rss_settings(user_id, feeds_enabled=new_status)
            
            await query.answer(f"Feeds {'enabled' if new_status else 'disabled'}!")
            await refresh_main_menu(message, user_id)
        
        elif data == "rss_toggle_livechart":
            settings = await get_livechartme_settings(user_id)
            new_status = not settings.get("enabled", False)
            await update_livechartme_settings(user_id, new_status)
            
            await query.answer(f"LiveChartMe {'enabled' if new_status else 'disabled'}!")
            await refresh_main_menu(message, user_id)
        
        elif data == "rss_lc_groups_menu":
            await show_lc_groups_menu(query)
        
        elif data.startswith("rss_lc_groups_list"):
            page = int(data.split()[1]) if len(data.split()) > 1 else 0
            await show_lc_groups_list(query, page)
        
        elif data == "rss_lc_groups_add":
            await query.answer()
            buttons = ButtonMaker()
            buttons.data_button("Back", "rss_lc_groups_menu")
            
            await message.edit_text(
                "**Add LiveChart.me Group**\n\n"
                "Reply to this message with the group ID.\n\n"
                "Example:\n"
                "`-1001234567890`\n\n"
                "**Note:** Bot must be added to the group first!",
                reply_markup=buttons.build_menu(1)
            )
        
        elif data == "rss_lc_groups_test":
            await query.answer("Fetching latest LiveChart.me headline...", show_alert=False)
            
            soup = await fetch_livechart_news()
            if not soup:
                await query.answer("Failed to fetch LiveChart feed!", show_alert=True)
                return
            
            first_item = soup.find('item')
            if not first_item:
                await query.answer("No items found in feed!", show_alert=True)
                return
            
            title_tag = first_item.find('title')
            link_tag = first_item.find('link')
            source_tag = first_item.find('guid')
            
            title = title_tag.get_text(strip=True) if title_tag else ""
            link = link_tag.get_text(strip=True) if link_tag else ""
            source_url = source_tag.get_text(strip=True) if source_tag else ""
            
            enclosure = first_item.find('enclosure')
            img_url = None
            if enclosure and enclosure.get('url'):
                img_url = str(enclosure.get('url')).split('?')[0]
            
            description = first_item.find('description')
            if description:
                desc_html = str(description)
          
            item = {
                'title': title,
                'link': link,
                'source': source_url,
                'image': img_url
            }
            
            groups = await get_user_groups(user_id)
            if not groups:
                await query.answer("No groups configured!", show_alert=True)
                return
            
            success_count = 0
            for group_doc in groups:
                success = await send_livechart_to_target(group_doc['group_id'], item)
                if success:
                    success_count += 1
                    await LIVECHARTME_GROUPS.update_one(
                        {"_id": group_doc['_id']},
                        {"$set": {"last_message": datetime.utcnow()}}
                    )
                await sleep(1.5)
            
            await query.answer(f"Test sent to {success_count}/{len(groups)} group(s)!", show_alert=True)
        
        elif data.startswith("rss_lc_group_remove"):
            group_doc_id = data.split(maxsplit=1)[1] if len(data.split()) > 1 else ""
            success, msg = await remove_livechart_group(user_id, group_doc_id)
            await query.answer(msg, show_alert=True)
            if success:
                await show_lc_groups_list(query, 0)
        
        elif data.startswith("rss_list"):
            page = int(data.split()[1]) if len(data.split()) > 1 else 0
            await show_feed_list(query, page)
        
        elif data == "rss_add":
            await query.answer()
            buttons = ButtonMaker()
            buttons.data_button("Back", "rss_back_main")
            
            await message.edit_text(
                "**Add New RSS Feed**\n\n"
                "Reply to this message with feed details in format:\n"
                "`Feed Title | Feed URL`\n\n"
                "Example:\n"
                "`Tech News | https://example.com/feed.xml`",
                reply_markup=buttons.build_menu(1)
            )
        
        elif data.startswith("rss_feed"):
            feed_id = data.split(maxsplit=1)[1] if len(data.split()) > 1 else ""
            await show_feed_options(query, feed_id)
        
        elif data.startswith("rss_toggle"):
            feed_id = data.split(maxsplit=1)[1] if len(data.split()) > 1 else ""
            success, msg, new_status = await toggle_feed(user_id, feed_id)
            await query.answer(msg)
            if success:
                await show_feed_options(query, feed_id)
        
        elif data.startswith("rss_remove"):
            feed_id = data.split(maxsplit=1)[1] if len(data.split()) > 1 else ""
            success, msg = await remove_feed(user_id, feed_id)
            await query.answer(msg, show_alert=True)
            if success:
                await show_feed_list(query, 0)
        
        elif data.startswith("rss_test"):
            feed_id = data.split(maxsplit=1)[1] if len(data.split()) > 1 else ""
            await query.answer("Testing feed...", show_alert=False)
            feed = await RSS_FEEDS.find_one({"_id": feed_id, "user_id": user_id})
            
            if feed:
                item = await process_rss_feed(feed, is_manual=True)
                if item:
                    await send_rss_message(
                        user_id,
                        item['title'],
                        item['link'],
                        feed['title'],
                        item.get('image')
                    )
                    await query.answer("Test message sent!", show_alert=True)
                else:
                    await query.answer("No items found or fetch failed", show_alert=True)
            else:
                await query.answer("Feed not found!", show_alert=True)
        
        elif data.startswith("rss_back_list"):
            page = int(data.split()[1]) if len(data.split()) > 1 else 0
            await show_feed_list(query, page)
        
        elif data == "rss_back_main":
            await refresh_main_menu(message, user_id)
    
    except Exception as e:
        LOGGER.error(f"RSS callback error: {e}", exc_info=True)
        await query.answer("An error occurred!", show_alert=True)


async def refresh_main_menu(message, user_id: int):
    rss_settings = await get_rss_settings(user_id)
    lc_settings = await get_livechartme_settings(user_id)
    feeds = await get_user_feeds(user_id)
    groups = await get_user_groups(user_id)
    
    buttons = ButtonMaker()
    
    feeds_status = "✅ Enabled" if rss_settings.get("feeds_enabled") else "❌ Disabled"
    buttons.data_button(f"Feeds: {feeds_status}", "rss_toggle_feeds")
    
    lc_status = "✅ Enabled" if lc_settings.get("enabled") else "❌ Disabled"
    buttons.data_button(f"LiveChartMe: {lc_status}", "rss_toggle_livechart")
    
    rss_status = "✅ Enabled" if rss_settings.get("rss_enabled") else "❌ Disabled"
    buttons.data_button(f"RSS: {rss_status}", "rss_toggle_main")
    
    buttons.data_button("LiveChartMe Groups", "rss_lc_groups_menu")
    buttons.data_button(f"My Feeds ({len(feeds)})", "rss_list 0")
    buttons.data_button("Add Feed", "rss_add")
    buttons.data_button("Close", "rss_close")
    
    text = (
        "**RSS Feed Manager**\n\n"
        "Manage your RSS feed subscriptions and get real-time updates.\n\n"
        f"**RSS Service:** {rss_status}\n"
        f"**Feeds Delivery:** {feeds_status}\n"
        f"**LiveChartMe Headlines:** {lc_status}\n"
        f"**Active Feeds:** {len(feeds)}\n"
        f"**LiveChart Groups:** {len(groups)}\n\n"
        "Enable RSS to start the monitoring service. "
        "Enable Feeds/LiveChartMe to receive updates."
    )
    
    await message.edit_text(text, reply_markup=buttons.build_menu(2))


async def show_lc_groups_menu(query):
    user_id = query.from_user.id
    groups = await get_user_groups(user_id)
    
    buttons = ButtonMaker()
    buttons.data_button(f"My Groups ({len(groups)})", "rss_lc_groups_list 0")
    buttons.data_button("Add Group", "rss_lc_groups_add")
    buttons.data_button("Test Now", "rss_lc_groups_test")
    buttons.data_button("Back", "rss_back_main")
    
    text = (
        "**Configure LiveChart.me Groups**\n\n"
        "Send LiveChart.me anime news headlines directly to your groups.\n\n"
        f"**Configured Groups:** {len(groups)}\n\n"
        "Add the bot to your group first, then add the group ID here."
    )
    
    await query.message.edit_text(text, reply_markup=buttons.build_menu(1))
    await query.answer()


async def show_lc_groups_list(query, page: int = 0):
    groups = await get_user_groups(query.from_user.id)
    
    total_pages = ceil(len(groups) / GROUPS_PER_PAGE) if groups else 1
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * GROUPS_PER_PAGE
    end_idx = start_idx + GROUPS_PER_PAGE
    page_groups = groups[start_idx:end_idx]
    
    buttons = ButtonMaker()
    
    if page_groups:
        for group in page_groups:
            title = group['group_title'][:30]
            buttons.data_button(
                f"❌ {title}",
                f"rss_lc_group_remove {group['_id']}"
            )
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(("Previous", f"rss_lc_groups_list {page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(("Next", f"rss_lc_groups_list {page+1}"))
    
    for btn_text, btn_data in nav_buttons:
        buttons.data_button(btn_text, btn_data)
    
    buttons.data_button("Add Group", "rss_lc_groups_add")
    buttons.data_button("Back", "rss_lc_groups_menu")
    
    text = "**LiveChart.me Groups**\n\n"
    if groups:
        text += f"You have **{len(groups)}** configured group(s).\n"
        if total_pages > 1:
            text += f"Page {page + 1}/{total_pages}\n"
        text += "\nTap ❌ to remove a group."
    else:
        text += "You don't have any groups configured yet.\n\n"
        text += "Click **Add Group** to add your first group!"
    
    await query.message.edit_text(text, reply_markup=buttons.build_menu(1))
    await query.answer()


async def show_feed_list(query, page: int = 0):
    feeds = await get_user_feeds(query.from_user.id)
    
    total_pages = ceil(len(feeds) / FEEDS_PER_PAGE) if feeds else 1
    page = max(0, min(page, total_pages - 1))
    
    start_idx = page * FEEDS_PER_PAGE
    end_idx = start_idx + FEEDS_PER_PAGE
    page_feeds = feeds[start_idx:end_idx]
    
    buttons = ButtonMaker()
    
    if page_feeds:
        for feed in page_feeds:
            status_emoji = "✅" if feed.get("enabled", True) else "❌"
            buttons.data_button(
                f"{status_emoji} {feed['title'][:30]}",
                f"rss_feed {feed['_id']}"
            )
    
    nav_buttons = []
    if page > 0:
        nav_buttons.append(("Previous", f"rss_list {page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(("Next", f"rss_list {page+1}"))
    
    for btn_text, btn_data in nav_buttons:
        buttons.data_button(btn_text, btn_data)
    
    buttons.data_button("Add Feed", "rss_add")
    buttons.data_button("Back", "rss_back_main")
    
    text = "**Your RSS Feeds**\n\n"
    if feeds:
        text += f"You have **{len(feeds)}** subscribed feed(s).\n"
        if total_pages > 1:
            text += f"Page {page + 1}/{total_pages}\n"
        text += "\nTap on a feed to manage it."
    else:
        text += "You don't have any subscribed feeds yet.\n\n"
        text += "Click **Add Feed** to subscribe to your first feed!"
    
    await query.message.edit_text(text, reply_markup=buttons.build_menu(1))
    await query.answer()


async def show_feed_options(query, feed_id: str):
    feed = await RSS_FEEDS.find_one({"_id": feed_id, "user_id": query.from_user.id})
    
    if not feed:
        await query.answer("Feed not found!", show_alert=True)
        return
    
    buttons = ButtonMaker()
    
    status = "✅ Enabled" if feed.get("enabled", True) else "❌ Disabled"
    buttons.data_button(f"Status: {status}", f"rss_toggle {feed_id}")
    buttons.data_button("Test Now", f"rss_test {feed_id}")
    buttons.data_button("Remove Feed", f"rss_remove {feed_id}")
    buttons.data_button("Back to List", "rss_back_list 0")
    
    feed_data = await RSS_FEED_DATA.find_one({"feed_id": feed_id})
    
    created_time = format_time(feed.get('created_at'))
    last_check = format_time(feed_data.get('last_checked')) if feed_data else "Never"
    
    text = f"**{feed['title']}**\n\n"
    text += f"**URL:** `{feed['url']}`\n"
    text += f"**Status:** {status}\n"
    text += f"**Added:** {created_time}\n"
    text += f"**Last Check:** {last_check}\n"
    
    if feed_data:
        total_items = feed_data.get('total_items', 0)
        check_count = feed_data.get('check_count', 0)
        success_count = feed_data.get('success_count', 0)
        
        text += f"**Items Received:** {total_items} total\n"
        
        if check_count > 0:
            success_rate = (success_count / check_count) * 100
            text += f"**Success Rate:** {success_rate:.1f}% ({success_count}/{check_count} checks)\n"
    
    text += "\nUse buttons below to manage this feed."
    
    await query.message.edit_text(text, reply_markup=buttons.build_menu(1))
    await query.answer()


async def rss_add_handler(client, message):
    if not message.reply_to_message:
        return
    
    reply_text = message.reply_to_message.text or ""
    
    if "Add New RSS Feed" in reply_text:
        text = message.text.strip()
        
        if '|' not in text:
            buttons = ButtonMaker()
            buttons.data_button("Back", "rss_back_main")
            
            await edit_message(
                message.reply_to_message,
                "Invalid format! Use:\n`Feed Title | Feed URL`",
                buttons.build_menu(1)
            )
            await delete_message(message)
            return
        
        parts = text.split('|', 1)
        title = parts[0].strip()
        url = parts[1].strip()
        
        if not title or not url:
            buttons = ButtonMaker()
            buttons.data_button("Back", "rss_back_main")
            
            await edit_message(
                message.reply_to_message,
                "Both title and URL are required!",
                buttons.build_menu(1)
            )
            await delete_message(message)
            return
        
        buttons = ButtonMaker()
        buttons.data_button("Back", "rss_back_main")
        
        await edit_message(message.reply_to_message, "Validating feed...", buttons.build_menu(1))
        success, result = await add_feed(message.from_user.id, url, title)
        
        await edit_message(message.reply_to_message, result, buttons.build_menu(1))
        await delete_message(message)
    
    elif "Add LiveChart.me Group" in reply_text:
        text = message.text.strip()
        
        try:
            group_id = int(text)
        except ValueError:
            buttons = ButtonMaker()
            buttons.data_button("Back", "rss_lc_groups_menu")
            
            await edit_message(
                message.reply_to_message,
                "Invalid group ID! Must be a number.\n\nExample: `-1001234567890`",
                buttons.build_menu(1)
            )
            await delete_message(message)
            return
        
        buttons = ButtonMaker()
        buttons.data_button("Back", "rss_lc_groups_menu")
        
        await edit_message(message.reply_to_message, "Validating group...", buttons.build_menu(1))
        success, result = await add_livechart_group(message.from_user.id, group_id)
        
        await edit_message(message.reply_to_message, result, buttons.build_menu(1))
        await delete_message(message)


bot.add_handler(
    MessageHandler(
        rss_menu,
        filters=command('rss', case_sensitive=True) & CustomFilters.authorized
    )
)

bot.add_handler(
    MessageHandler(
        rss_add_handler,
        filters=filters.text & filters.reply & CustomFilters.authorized
    )
)

bot.add_handler(
    CallbackQueryHandler(
        rss_callback,
        filters=filters.regex(r"^rss_")
    )
)


def add_job():
    scheduler.add_job(
        rss_monitor,
        trigger=IntervalTrigger(seconds=RSS_DELAY),
        id="rss_monitor",
        name="RSS",
        misfire_grace_time=15,
        max_instances=1,
        next_run_time=datetime.now() + timedelta(seconds=20),
        replace_existing=True,
    )

add_job()
scheduler.start()