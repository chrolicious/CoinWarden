-- CoinWarden: passively logs gold and auction activity into SavedVariables.
-- No network access (addons can't make HTTP calls) - a local companion
-- script reads this file and syncs it to the CoinWarden dashboard.
--
-- All AH buys and sells deliver through in-game mail (confirmed live), so
-- GetInboxInvoiceInfo on mail open/update is the single source for both -
-- no chat-parsing or purchase-event guessing needed. Field mapping
-- (price/quantity/deposit/ah_cut) confirmed live 2026-09-17 against known
-- sale amounts.

local ADDON_NAME = ...
local EVENT_CAP = 500 -- per character; oldest trimmed first, sync script should pull often

local f = CreateFrame("Frame")
local playerKey, realmName

local function nowTS()
    return time()
end

local function ensureCharacter()
    CoinWardenDB = CoinWardenDB or { version = 1, characters = {} }
    CoinWardenDB.characters[playerKey] = CoinWardenDB.characters[playerKey] or {
        realm = realmName,
        class = select(2, UnitClass("player")),
        gold = 0,
        gold_updated_at = nil,
        auctions = {},
        auctions_updated_at = nil,
        events = {},
    }
    return CoinWardenDB.characters[playerKey]
end

local function pushEvent(kind, data)
    local char = ensureCharacter()
    char.next_event_id = (char.next_event_id or 0) + 1
    data.event_id = char.next_event_id
    data.type = kind
    data.ts = nowTS()
    table.insert(char.events, data)
    local overflow = #char.events - EVENT_CAP
    if overflow > 0 then
        for i = 1, overflow do
            table.remove(char.events, 1)
        end
    end
end

local function snapshotGold()
    local char = ensureCharacter()
    char.gold = GetMoney()
    char.gold_updated_at = nowTS()
end

-- Warband Bank gold isn't visible to GetMoney() (that's character-only).
-- C_Bank.FetchDepositedMoney(Enum.BankType.Account) confirmed live
-- 2026-09-17 - correct value read back on the first guess.
local function snapshotWarbandGold()
    if not (C_Bank and C_Bank.FetchDepositedMoney and Enum.BankType) then
        return
    end
    local char = ensureCharacter()
    char.warband_gold = C_Bank.FetchDepositedMoney(Enum.BankType.Account)
    char.warband_gold_updated_at = nowTS()
end

local function snapshotOwnedAuctions()
    if not (C_AuctionHouse and C_AuctionHouse.GetOwnedAuctions) then
        return
    end
    local char = ensureCharacter()
    local owned = C_AuctionHouse.GetOwnedAuctions()
    local out = {}
    for _, entry in ipairs(owned) do
        out[entry.auctionID] = {
            itemID = entry.itemKey and entry.itemKey.itemID or nil,
            itemLink = entry.itemLink,
            quantity = entry.quantity,
            buyoutAmount = entry.buyoutAmount,
            bidAmount = entry.bidAmount,
            timeLeftSeconds = entry.timeLeftSeconds,
            status = entry.status,
        }
    end
    char.auctions = out
    char.auctions_updated_at = nowTS()
end

-- AH-sold mail carries an explicit invoice Blizzard fills in for exactly
-- this purpose (used by every AH addon for years) - far more reliable than
-- parsing chat text.
--
-- Mail sits in the inbox until claimed, and MAIL_INBOX_UPDATE re-fires on
-- every inbox refresh - so the same pending invoice gets rescanned many
-- times. Dedup must be permanent (persisted), not a short time window: a
-- 30s window only caught duplicates within one visit and still logged the
-- same unclaimed mail again on the next visit (confirmed live - the same
-- handful of invoices appeared dozens of times in one dump).
local function scanMailInvoices()
    local char = ensureCharacter()
    char.seenInvoices = char.seenInvoices or {}
    local num = GetInboxNumItems()
    for i = 1, num do
        local v1, v2, v3, price, qty, deposit, ahCut, v8, unclaimed, v10 = GetInboxInvoiceInfo(i)
        if v1 == "seller" or v1 == "buyer" then
            local key = table.concat({ v1, tostring(v2), tostring(v3), tostring(price), tostring(qty) }, "|")
            if not char.seenInvoices[key] then
                char.seenInvoices[key] = nowTS()
                -- The confirmed-live quantity slot (v5) held the actual
                -- stack size for a bid-won stackable item, but a straight
                -- buyout of a one-of-a-kind item (e.g. a Design) returned
                -- the same value as price there instead - no real WoW AH
                -- stack ever approaches that size, so treat anything above
                -- the realistic max stack (1000) as "unknown, assume 1"
                -- rather than trust a clearly bogus number.
                local quantity = (type(qty) == "number" and qty > 0 and qty <= 1000) and qty or 1
                pushEvent(v1 == "seller" and "sold" or "bought_via_mail", {
                    item_name = v2, counterparty = v3,
                    price_copper = price, quantity = quantity, quantity_raw = qty,
                    deposit = deposit, ah_cut = ahCut, unclaimed_money = unclaimed, v8 = v8, v10 = v10,
                })
            end
        end
    end
end

-- Every gold change gets categorized and logged as its own event, so the
-- dashboard can build a real income/expense breakdown instead of just a
-- net-worth line. Categories, in priority order when several could apply:
--   quest_reward - exact, from QUEST_TURNED_IN's own moneyReward arg
--   repair       - exact, from hooking RepairAllItems/RepairItem
--   ah_related   - AH frame or mailbox was open; the precise transaction is
--                  already logged separately via scanMailInvoices/owned
--                  auctions, so this exists only to keep it OUT of "other"
--   vendor_sell/vendor_buy - merchant window was open (direction from sign)
--   loot         - loot window was open
--   other        - none of the above; genuinely uncategorized (world quest
--                  currency vendors, misc NPC interactions, etc.)
local lastGold
local pendingQuestReward = false
local pendingRepair = false
local auctionHouseOpen, mailOpen, merchantOpen, lootOpen = false, false, false, false
-- A deposit/purchase charge can land a moment after the triggering frame's
-- close event fires (confirmed live: an ~8000g AH deposit got tagged
-- "other" because AUCTION_HOUSE_CLOSED had already flipped the flag off by
-- the time the charge posted). Grace period keeps "just closed" counted as
-- still open for categorization purposes.
local CLOSE_GRACE_SECONDS = 5
local auctionHouseClosedAt, mailClosedAt, merchantClosedAt

local function recentlyOpen(isOpen, closedAt)
    return isOpen or (closedAt and nowTS() - closedAt <= CLOSE_GRACE_SECONDS)
end

local function handleMoneyChange()
    local char = ensureCharacter()
    local newGold = GetMoney()
    if lastGold == nil then
        lastGold = newGold
        snapshotGold()
        return
    end
    local delta = newGold - lastGold
    lastGold = newGold
    snapshotGold()
    if delta == 0 then return end

    local category
    if pendingQuestReward then
        category = "quest_reward"
    elseif pendingRepair then
        category = "repair"
    elseif recentlyOpen(auctionHouseOpen, auctionHouseClosedAt) or recentlyOpen(mailOpen, mailClosedAt) then
        category = "ah_related"
    elseif recentlyOpen(merchantOpen, merchantClosedAt) then
        category = delta > 0 and "vendor_sell" or "vendor_buy"
    elseif lootOpen then
        category = "loot"
    else
        category = "other"
    end
    pendingQuestReward, pendingRepair = false, false
    pushEvent("gold_delta", { delta = delta, category = category })
end

if RepairAllItems then
    hooksecurefunc("RepairAllItems", function() pendingRepair = true end)
end
if RepairItem then
    hooksecurefunc("RepairItem", function() pendingRepair = true end)
end

f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("PLAYER_LOGIN")
f:RegisterEvent("PLAYER_MONEY")
f:RegisterEvent("MAIL_SHOW")
f:RegisterEvent("MAIL_CLOSED")
f:RegisterEvent("MAIL_INBOX_UPDATE")
f:RegisterEvent("AUCTION_HOUSE_SHOW")
f:RegisterEvent("AUCTION_HOUSE_CLOSED")
f:RegisterEvent("OWNED_AUCTIONS_UPDATED")
f:RegisterEvent("MERCHANT_SHOW")
f:RegisterEvent("MERCHANT_CLOSED")
f:RegisterEvent("LOOT_OPENED")
f:RegisterEvent("LOOT_CLOSED")
f:RegisterEvent("QUEST_TURNED_IN")
f:RegisterEvent("BANKFRAME_OPENED")
f:RegisterEvent("PLAYERBANKSLOTS_CHANGED")

f:SetScript("OnEvent", function(self, event, arg1, arg2, arg3)
    if event == "ADDON_LOADED" then
        if arg1 ~= ADDON_NAME then return end
        playerKey = UnitName("player") .. "-" .. GetRealmName()
        realmName = GetRealmName()
        ensureCharacter()
        lastGold = GetMoney()
        snapshotGold()
    elseif event == "PLAYER_LOGIN" then
        lastGold = GetMoney()
        snapshotGold()
    elseif event == "PLAYER_MONEY" then
        handleMoneyChange()
    elseif event == "MAIL_SHOW" then
        mailOpen = true
        local ok, err = pcall(scanMailInvoices)
        if not ok then pushEvent("_error", { context = "scanMailInvoices", err = tostring(err) }) end
    elseif event == "MAIL_INBOX_UPDATE" then
        local ok, err = pcall(scanMailInvoices)
        if not ok then pushEvent("_error", { context = "scanMailInvoices", err = tostring(err) }) end
    elseif event == "MAIL_CLOSED" then
        mailOpen = false
        mailClosedAt = nowTS()
    elseif event == "AUCTION_HOUSE_SHOW" then
        auctionHouseOpen = true
        local ok, err = pcall(snapshotOwnedAuctions)
        if not ok then pushEvent("_error", { context = "snapshotOwnedAuctions", err = tostring(err) }) end
    elseif event == "OWNED_AUCTIONS_UPDATED" then
        local ok, err = pcall(snapshotOwnedAuctions)
        if not ok then pushEvent("_error", { context = "snapshotOwnedAuctions", err = tostring(err) }) end
    elseif event == "AUCTION_HOUSE_CLOSED" then
        auctionHouseOpen = false
        auctionHouseClosedAt = nowTS()
    elseif event == "MERCHANT_SHOW" then
        merchantOpen = true
    elseif event == "MERCHANT_CLOSED" then
        merchantOpen = false
        merchantClosedAt = nowTS()
    elseif event == "LOOT_OPENED" then
        lootOpen = true
    elseif event == "LOOT_CLOSED" then
        lootOpen = false
    elseif event == "QUEST_TURNED_IN" then
        -- Signature assumed as (questID, xpReward, moneyReward) - not yet
        -- confirmed live. If pendingQuestReward never actually triggers on
        -- a real quest turn-in with a gold reward, the delta will just fall
        -- through to "other" instead - harmless, just less precise than
        -- intended. Check with /coinwarden dump after turning in a quest
        -- that pays gold.
        local moneyReward = arg3
        if moneyReward and moneyReward > 0 then
            pendingQuestReward = true
        end
    elseif event == "BANKFRAME_OPENED" or event == "PLAYERBANKSLOTS_CHANGED" then
        local ok, err = pcall(snapshotWarbandGold)
        if not ok then pushEvent("_error", { context = "snapshotWarbandGold", err = tostring(err) }) end
    end
end)

-- /coinwarden status | dump [n] | reset
SLASH_COINWARDEN1 = "/coinwarden"
SlashCmdList["COINWARDEN"] = function(msg)
    local cmd, rest = msg:match("^(%S*)%s*(.-)$")
    local char = ensureCharacter()
    if cmd == "dump" then
        local n = tonumber(rest) or 10
        local start = math.max(1, #char.events - n + 1)
        for i = start, #char.events do
            local e = char.events[i]
            local parts = {}
            for k, v in pairs(e) do
                table.insert(parts, k .. "=" .. tostring(v))
            end
            print("|cff33ff99CoinWarden|r " .. table.concat(parts, ", "))
        end
        print(("|cff33ff99CoinWarden|r %d/%d events shown"):format(#char.events - start + 1, #char.events))
    elseif cmd == "reset" then
        char.events = {}
        char.seenInvoices = {}
        lastGold = GetMoney()
        print("|cff33ff99CoinWarden|r events cleared")
    else
        local auctionCount = 0
        for _ in pairs(char.auctions or {}) do auctionCount = auctionCount + 1 end
        local warband = char.warband_gold and (" warband=%dg"):format(char.warband_gold / 10000) or " warband=unconfirmed"
        print(("|cff33ff99CoinWarden|r gold=%dg auctions=%d events=%d%s"):format(
            (char.gold or 0) / 10000, auctionCount, #char.events, warband))
    end
end
