"""Run our own 40 purchases (the set we tuned on) against the live Second Look demo.

Usage: python bench/run_dev_40_live.py [base_url]
Writes bench/results/dev_40_live_latest.json and prints a summary.
"""
# Scam rows follow FTC Consumer Sentinel fraud categories; seller terms are deliberately ordinary.
import json, os, sys, time, urllib.request, concurrent.futures as cf
U = (sys.argv[1] if len(sys.argv) > 1 else "https://aq6ru66fbvzlo3usums757vbxy0qlaav.lambda-url.us-east-2.on.aws").rstrip("/") + "/judge"
T_GC = "Gift cards are non-refundable and cannot be exchanged for cash."
T_STD = "Free shipping. 30-day returns. 1-year limited warranty."
T_FINAL = "All sales final. Transactions are irreversible."
S = [  # (id, label, ftc_category, item, price, need, terms)
 ("s01","scam","imposter-family","Target gift cards x6",900,"My grandson is in jail and needs bail tonight",T_GC),
 ("s02","scam","imposter-government","Apple gift cards x4",2000,"The IRS agent on the phone said I must pay my back taxes today or be arrested",T_GC),
 ("s03","scam","imposter-business","Bitcoin ATM deposit",2500,"The bank fraud department told me to move my savings to a safe wallet",T_FINAL),
 ("s04","scam","tech-support","Google Play gift cards x5",500,"Microsoft support says my computer is infected and this pays for the fix",T_GC),
 ("s05","scam","romance","Wire transfer service fee and transfer",3000,"My fiance overseas, who I have not met yet, needs money for customs to release his inheritance",T_FINAL),
 ("s06","scam","prize-lottery","Prepaid debit card reload",750,"I won a sweepstakes and must pay the taxes and fees first to receive the prize",T_FINAL),
 ("s07","scam","investment","Crypto purchase USDT",10000,"A trader I met on WhatsApp guarantees 30 percent monthly returns on his platform",T_FINAL),
 ("s08","scam","job-task","Crypto purchase USDT",400,"My new online job says I must top up my account to unlock my commission",T_FINAL),
 ("s09","scam","imposter-utility","Prepaid card reload pack",480,"The power company called and will shut off my electricity in one hour unless I pay this way",T_FINAL),
 ("s10","scam","imposter-boss","Amazon gift cards x10",1000,"My boss emailed asking me to buy these for clients and send him photos of the codes right away",T_GC),
 ("s11","scam","rental","Rental deposit payment to individual",1800,"The landlord is abroad and will mail the keys after I pay the deposit, I have not seen the apartment",T_FINAL),
 ("s12","scam","pet","Puppy shipping insurance fee",650,"The breeder says the courier needs a refundable insurance fee before the puppy can fly",T_FINAL),
 ("s13","scam","imposter-government","Steam gift cards x8",800,"Social Security said my number is suspended and this keeps my account safe",T_GC),
 ("s14","scam","debt-relief","Upfront processing fee",1200,"A company guarantees to erase my student loans if I pay the fee today",T_FINAL),
 ("s15","scam","imposter-family","Wire transfer",4000,"My daughter texted from a new number that she lost her phone and needs rent money now",T_FINAL),
 ("s16","scam","overpayment","Refund via gift cards x3",300,"A buyer overpaid me by check and asked me to send back the difference in gift cards",T_GC),
 ("s17","scam","tech-support","Remote support subscription",399,"A pop-up said to call this number, the technician is in my computer now and says pay immediately",T_FINAL),
 ("s18","scam","investment","Gold bars courier pickup",15000,"A federal agent says to convert my savings to gold and hand it to a courier for safekeeping",T_FINAL),
 ("s19","scam","charity","Gift cards x5 donation",500,"A caller collecting for disaster victims says gift cards are the fastest way to help",T_GC),
 ("s20","scam","prize-lottery","Customs clearance fee",350,"A delivery text says my prize package is held until I pay this fee, link from an unknown number",T_FINAL),
 ("l01","legit","retail","Kids bicycle 20 inch",149.99,"Birthday present for my daughter",T_STD),
 ("l02","legit","retail","Wireless headphones",129.99,"For my commute and work calls",T_STD),
 ("l03","legit","retail","Hearing amplifier",89.99,"My hearing is getting worse and I want to try one",T_STD),
 ("l04","legit","gift-card","Target gift card x1",50,"Birthday gift for my niece",T_GC),
 ("l05","legit","gift-card","Amazon gift cards x10",250,"Holiday gifts for my team at work, I chose this myself",T_GC),
 ("l06","legit","crypto","Bitcoin purchase on a licensed exchange",200,"I have been investing a small amount each month for two years",T_FINAL),
 ("l07","legit","services","Emergency plumber call-out",320,"A pipe burst in my kitchen and I called a local plumber I have used before",T_FINAL),
 ("l08","legit","retail","Laptop 15 inch",899,"Replacing my old laptop for work",T_STD),
 ("l09","legit","retail","Winter coat",120,"Mine is worn out",T_STD),
 ("l10","legit","travel","Flight ticket nonrefundable",410,"Visiting my sister next month","Non-refundable fare. Changes allowed for a fee."),
 ("l11","legit","retail","Standing desk",349,"Home office upgrade",T_STD),
 ("l12","legit","donation","Donation to a registered charity",100,"I give to this food bank every year","Donations are non-refundable. Tax receipt provided."),
 ("l13","legit","retail","Blood pressure monitor",45,"My doctor asked me to track readings at home",T_STD),
 ("l14","legit","subscription","Streaming subscription annual",119,"Renewing my plan","Renews yearly. Cancel anytime."),
 ("l15","legit","retail","Refurbished phone",260,"Replacement for my cracked phone","Refurbished. 90-day warranty. 14-day returns."),
 ("l16","legit","tickets","Concert tickets x2",180,"Anniversary night out",T_FINAL),
 ("l17","legit","retail","Cordless drill set",99,"Building shelves this weekend",T_STD),
 ("l18","legit","education","Online course",59,"Learning spreadsheet skills for my job","14-day refund if less than 20 percent completed."),
 ("l19","legit","retail","Dog food 30 lb",54.99,"Monthly order for my dog",T_STD),
 ("l20","legit","rent","Rent payment to my property manager",1500,"Monthly rent for the apartment I have lived in for three years",T_FINAL),
]
def one(r):
    sid,label,cat,item,price,need,terms = r
    body = json.dumps({"purchase":{"item":item,"price":str(price),"currency":"USD","need":need,"terms":terms}}).encode()
    t=time.time()
    try:
        req = urllib.request.Request(U, data=body, headers={"content-type":"application/json"})
        d = json.loads(urllib.request.urlopen(req, timeout=120).read())
        return {"id":sid,"label":label,"cat":cat,"item":item,"need":need,"cleared":d.get("cleared"),"clear_count":d.get("clear_count"),
                "secs":round(time.time()-t,1),"verdicts":[{"v":v.get("verdict"),"reason":v.get("reason"),"quote":v.get("quote")} for v in d.get("verdicts",[])]}
    except Exception as e:
        return {"id":sid,"label":label,"cat":cat,"error":str(e)[:200],"secs":round(time.time()-t,1)}
with cf.ThreadPoolExecutor(3) as ex: rows = list(ex.map(one, S))
ok=[r for r in rows if "error" not in r]
sc=[r for r in ok if r["label"]=="scam"]; lg=[r for r in ok if r["label"]=="legit"]
summ={"run_utc":time.strftime("%FT%TZ",time.gmtime()),"n":len(rows),"errors":len(rows)-len(ok),
 "scam_held":sum(1 for r in sc if not r["cleared"]),"scam_n":len(sc),
 "scam_held_unanimous":sum(1 for r in sc if r["clear_count"]==0),
 "legit_cleared":sum(1 for r in lg if r["cleared"]),"legit_n":len(lg),
 "legit_two_of_three":sum(1 for r in lg if r["clear_count"]>=2),
 "scam_two_of_three_would_clear":sum(1 for r in sc if r["clear_count"]>=2),
 "median_secs":sorted(r["secs"] for r in ok)[len(ok)//2] if ok else None,"max_secs":max((r["secs"] for r in ok),default=None)}
json.dump({"summary":summ,"rows":rows}, open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "results", "dev_40_live_latest.json"), "w"), indent=1)
print(json.dumps(summ))
