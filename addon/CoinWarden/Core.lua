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
                pushEvent(v1 == "seller" and "sold" or "bought_via_mail", {
                    item_name = v2, counterparty = v3,
                    price_copper = price, quantity = qty, deposit = deposit, ah_cut = ahCut,
                    unclaimed_money = unclaimed, v8 = v8, v10 = v10,
                })
            end
        end
    end
end

f:RegisterEvent("ADDON_LOADED")
f:RegisterEvent("PLAYER_LOGIN")
f:RegisterEvent("PLAYER_MONEY")
f:RegisterEvent("MAIL_SHOW")
f:RegisterEvent("MAIL_INBOX_UPDATE")
f:RegisterEvent("AUCTION_HOUSE_SHOW")
f:RegisterEvent("OWNED_AUCTIONS_UPDATED")

f:SetScript("OnEvent", function(self, event, arg1, ...)
    if event == "ADDON_LOADED" then
        if arg1 ~= ADDON_NAME then return end
        playerKey = UnitName("player") .. "-" .. GetRealmName()
        realmName = GetRealmName()
        ensureCharacter()
        snapshotGold()
    elseif event == "PLAYER_LOGIN" then
        snapshotGold()
    elseif event == "PLAYER_MONEY" then
        snapshotGold()
    elseif event == "MAIL_SHOW" or event == "MAIL_INBOX_UPDATE" then
        local ok, err = pcall(scanMailInvoices)
        if not ok then
            pushEvent("_error", { context = "scanMailInvoices", err = tostring(err) })
        end
    elseif event == "AUCTION_HOUSE_SHOW" or event == "OWNED_AUCTIONS_UPDATED" then
        local ok, err = pcall(snapshotOwnedAuctions)
        if not ok then
            pushEvent("_error", { context = "snapshotOwnedAuctions", err = tostring(err) })
        end
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
        print("|cff33ff99CoinWarden|r events cleared")
    else
        local auctionCount = 0
        for _ in pairs(char.auctions or {}) do auctionCount = auctionCount + 1 end
        print(("|cff33ff99CoinWarden|r gold=%dg auctions=%d events=%d"):format(
            (char.gold or 0) / 10000, auctionCount, #char.events))
    end
end
