# Potential Issues with the Proposed Support Workflow

1. **Twitter API Throughput**
   Автоматическая проверка лайков/ретвитов снимает нагрузку с модераторов, но теперь всё упирается в лимиты Twitter API. Если участников много или доступ только на базовом тарифе, проверки могут задерживаться или блокироваться целиком.

2. **Twitter API Limitations**  
   Many engagement signals (likes, comments, reposts) are only accessible through the paid Twitter API tiers. If members support posts in ways that the bot cannot verify programmatically, legitimate users may stay muted.

3. **Synchronization Delays**  
   There can be a delay between when a user performs a support action and when the Twitter API reports it. Users may get frustrated if they are still muted while the API has not updated yet.

4. **Edge Cases with Deleted or Private Tweets**  
   If a tweet gets deleted, made private, or the account is suspended, the bot can no longer verify engagement. Participants might be blocked even though they supported the post when it was available.

5. **Handling Threaded Conversations**  
   Some influencers share threads or quote tweets. Tracking exactly "ten posts above" becomes ambiguous when posts reference multiple URLs or when several links appear in one message.

6. **Multi-Account Abuse**  
   Users could register multiple Telegram accounts to bypass the limit. Without additional identity checks, the system may struggle to enforce fairness.

7. **Time-Zone and Daily Reset Complications**  
   Enforcing the "two posts per day" rule requires a clear reset time. Participants in different time zones may find the cut-off confusing unless the bot communicates it explicitly.

8. **Support Verification for Old Messages**  
   When a user returns after some time, the ten messages above might now be days old. Ensuring those tweets still exist and can be supported may not be realistic.

9. **Twitter Account Linking Reliability**  
   If a user changes their Twitter handle after registering, the bot must handle the update gracefully. Otherwise, verification requests will fail.

10. **Telegram Mute Management**  
   The bot needs elevated admin rights to mute/unmute. Any change in group permissions or admin rights can break the workflow and leave users stuck.

11. **False Positives and Appeals**  
   Automated checks may occasionally fail. Providing a manual override or appeal process is important to prevent frustration.

12. **Scalability and Performance**  
   For a large community, maintaining per-user state, fetching tweet data, and reacting to each Telegram message in real time can strain the bot's resources.

