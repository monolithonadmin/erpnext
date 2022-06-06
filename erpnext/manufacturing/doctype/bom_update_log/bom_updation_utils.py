# Copyright (c) 2022, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

import json
from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Union

if TYPE_CHECKING:
	from erpnext.manufacturing.doctype.bom_update_log.bom_update_log import BOMUpdateLog

import frappe
from frappe import _


def replace_bom(boms: Dict) -> None:
	"Replace current BOM with new BOM in parent BOMs."

	current_bom = boms.get("current_bom")
	new_bom = boms.get("new_bom")

	unit_cost = get_bom_unit_cost(new_bom)
	update_new_bom_in_bom_items(unit_cost, current_bom, new_bom)

	frappe.cache().delete_key("bom_children")
	parent_boms = get_ancestor_boms(new_bom)

	for bom in parent_boms:
		bom_obj = frappe.get_doc("BOM", bom)
		# this is only used for versioning and we do not want
		# to make separate db calls by using load_doc_before_save
		# which proves to be expensive while doing bulk replace
		bom_obj._doc_before_save = bom_obj
		bom_obj.update_exploded_items()
		bom_obj.calculate_cost()
		bom_obj.update_parent_cost()
		bom_obj.db_update()
		if bom_obj.meta.get("track_changes") and not bom_obj.flags.ignore_version:
			bom_obj.save_version()


def update_cost_in_level(
	doc: "BOMUpdateLog", bom_list: List[str], batch_name: Union[int, str]
) -> None:
	"Updates Cost for BOMs within a given level. Runs via background jobs."

	try:
		status = frappe.db.get_value("BOM Update Log", doc.name, "status")
		if status == "Failed":
			return

		frappe.db.auto_commit_on_many_writes = 1

		update_cost_in_boms(bom_list=bom_list)  # main updation logic

		bom_batch = frappe.qb.DocType("BOM Update Batch")
		(
			frappe.qb.update(bom_batch)
			.set(bom_batch.boms_updated, json.dumps(bom_list))
			.set(bom_batch.status, "Completed")
			.where(bom_batch.name == batch_name)
		).run()
	except Exception:
		handle_exception(doc)
	finally:
		frappe.db.auto_commit_on_many_writes = 0
		frappe.db.commit()  # nosemgrep


def get_ancestor_boms(new_bom: str, bom_list: Optional[List] = None) -> List:
	"Recursively get all ancestors of BOM."

	bom_list = bom_list or []
	bom_item = frappe.qb.DocType("BOM Item")

	parents = (
		frappe.qb.from_(bom_item)
		.select(bom_item.parent)
		.where((bom_item.bom_no == new_bom) & (bom_item.docstatus < 2) & (bom_item.parenttype == "BOM"))
		.run(as_dict=True)
	)

	for d in parents:
		if new_bom == d.parent:
			frappe.throw(_("BOM recursion: {0} cannot be child of {1}").format(new_bom, d.parent))

		bom_list.append(d.parent)
		get_ancestor_boms(d.parent, bom_list)

	return list(set(bom_list))


def update_new_bom_in_bom_items(unit_cost: float, current_bom: str, new_bom: str) -> None:
	bom_item = frappe.qb.DocType("BOM Item")
	(
		frappe.qb.update(bom_item)
		.set(bom_item.bom_no, new_bom)
		.set(bom_item.rate, unit_cost)
		.set(bom_item.amount, (bom_item.stock_qty * unit_cost))
		.where(
			(bom_item.bom_no == current_bom) & (bom_item.docstatus < 2) & (bom_item.parenttype == "BOM")
		)
	).run()


def get_bom_unit_cost(bom_name: str) -> float:
	bom = frappe.qb.DocType("BOM")
	new_bom_unitcost = (
		frappe.qb.from_(bom).select(bom.total_cost / bom.quantity).where(bom.name == bom_name).run()
	)

	return frappe.utils.flt(new_bom_unitcost[0][0])


def update_cost_in_boms(bom_list: List[str]) -> None:
	"Updates cost in given BOMs. Returns current and total updated BOMs."

	for index, bom in enumerate(bom_list):
		bom_doc = frappe.get_doc("BOM", bom, for_update=True)
		bom_doc.calculate_cost(save_updates=True, update_hour_rate=True)
		bom_doc.db_update()

		if index % 100 == 0:
			frappe.db.commit()


def get_next_higher_level_boms(
	child_boms: List[str], processed_boms: Dict[str, bool]
) -> List[str]:
	"Generate immediate higher level dependants with no unresolved dependencies (children)."

	def _all_children_are_processed(parent_bom):
		child_boms = dependency_map.get(parent_bom)
		return all(processed_boms.get(bom) for bom in child_boms)

	dependants_map, dependency_map = _generate_dependence_map()

	dependants = []
	for bom in child_boms:
		# generate list of immediate dependants
		parents = dependants_map.get(bom) or []
		dependants.extend(parents)

	dependants = set(dependants)  # remove duplicates
	resolved_dependants = set()

	# consider only if children are all resolved
	for parent_bom in dependants:
		if _all_children_are_processed(parent_bom):
			resolved_dependants.add(parent_bom)

	return list(resolved_dependants)


def get_leaf_boms() -> List[str]:
	"Get BOMs that have no dependencies."

	return frappe.db.sql_list(
		"""select name from `tabBOM` bom
		where docstatus=1 and is_active=1
			and not exists(select bom_no from `tabBOM Item`
				where parent=bom.name and ifnull(bom_no, '')!='')"""
	)


def _generate_dependence_map() -> defaultdict:
	"""
	Generate maps such as: { BOM-1: [Dependant-BOM-1, Dependant-BOM-2, ..] }.
	Here BOM-1 is the leaf/lower level node/dependency.
	The list contains one level higher nodes/dependants that depend on BOM-1.

	Generate and return the reverse as well.
	"""

	bom = frappe.qb.DocType("BOM")
	bom_item = frappe.qb.DocType("BOM Item")

	bom_items = (
		frappe.qb.from_(bom_item)
		.join(bom)
		.on(bom_item.parent == bom.name)
		.select(bom_item.bom_no, bom_item.parent)
		.where(
			(bom_item.bom_no.isnotnull())
			& (bom_item.bom_no != "")
			& (bom.docstatus == 1)
			& (bom.is_active == 1)
			& (bom_item.parenttype == "BOM")
		)
	).run(as_dict=True)

	child_parent_map = defaultdict(list)
	parent_child_map = defaultdict(list)
	for row in bom_items:
		child_parent_map[row.bom_no].append(row.parent)
		parent_child_map[row.parent].append(row.bom_no)

	return child_parent_map, parent_child_map


def set_values_in_log(log_name: str, values: Dict[str, Any], commit: bool = False) -> None:
	"Update BOM Update Log record."

	if not values:
		return

	bom_update_log = frappe.qb.DocType("BOM Update Log")
	query = frappe.qb.update(bom_update_log).where(bom_update_log.name == log_name)

	for key, value in values.items():
		query = query.set(key, value)
	query.run()

	if commit:
		frappe.db.commit()  # nosemgrep


def handle_exception(doc: "BOMUpdateLog") -> None:
	"Rolls back and fails BOM Update Log."

	frappe.db.rollback()
	error_log = doc.log_error("BOM Update Tool Error")
	set_values_in_log(doc.name, {"status": "Failed", "error_log": error_log.name})
