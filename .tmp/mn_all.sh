#!/bin/bash
LOG=/scratch/mn_results.txt; : > $LOG
S=/scratch/bench/mn/commits
mk() { rm -rf /tmp/m; cp -r /src /tmp/m; }
cust() { # name, sed expr
  mk; sed -i "$2" /tmp/m/sqlglot/parser.py; cmp -s /tmp/m/sqlglot/parser.py /src/sqlglot/parser.py && echo "NOCHANGE $1" >> $LOG
  echo "== $1" >> $LOG; python /repo/.tmp/mn_one.py /tmp/m 2>&1 | grep RESULT >> $LOG; }
cust c1_inc0 '2201s/+= 1/+= 0/'
cust c2_ge '2202s/>/>=/'
cust c3_msg '2203s/({self.max_nodes})/({self.max_nodes + 1})/'
cust c5_inc2 '2201s/+= 1/+= 2/'
for k in 06mj 1lzm 34bs 5pfk 9ndw 9u7n b62z fmmy fu0f fvo0 lja1 mji0 o9si oic2 r8io v2y6 x8fo wvm9 0ozb v79r yazh; do
  mk; (cd /tmp/m && patch -p1 < $S/$k.patch >/dev/null) || echo "PATCHFAIL $k" >> $LOG
  echo "== $k" >> $LOG; python /repo/.tmp/mn_one.py /tmp/m 2>&1 | grep RESULT >> $LOG
done
echo DONE >> $LOG
