"""python main.py                 -> detect symptoms from Prometheus, then investigate
   python main.py "free text"    -> investigate this alert text
   python main.py --features localizer,falsify "free text"   -> choose techniques for this run
       (arms: llm-only | localizer | falsify | memory, cumulative; or a list from localizer,falsify,memory; default = env)
   python main.py --tuning tool_budget=20,report_words=100 "free text"   -> set the numbers behind the techniques (see sre_agent/tuning.py)"""
import sys
from dotenv import load_dotenv
load_dotenv()
from sre_agent.detect import find_problems
from sre_agent.features import parse as parse_features
from sre_agent.runner import run_alert
from sre_agent.tuning import parse as parse_tuning

if __name__ == "__main__":
    argv, feats, tune = sys.argv[1:], None, None
    while argv[:1] in (["--features"], ["--tuning"]) and len(argv) > 1:        # either flag, any order, before the alert text
        if argv[0] == "--features":
            feats = parse_features(argv[1])
        else:
            tune = parse_tuning(argv[1])
        argv = argv[2:]
    alert = " ".join(argv) or "; ".join(find_problems()) or "no active symptoms detected"
    print(f"ALERT: {alert}")
    r = run_alert(alert, features=feats, tuning=tune)
    print("\n" + r["final"])
    print(f"\nlog: {r['stats']['log_file']}")
    print(f"\n⏱ {r['seconds']}s | RAG hit: {r['rag_hit']} | actions: {[(a['tool'], a['approved']) for a in r['actions']]}")
