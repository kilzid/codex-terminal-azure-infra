"""Normalize documented ARM preview noise without accepting unknown drift."""
import copy
import re

# Only service-generated fields omitted by PUT, never desired security settings.
READ_ONLY = {
    'microsoft.network/networkinterfaces': {r'kind', r'properties\.allowPort25Out',
        r'properties.ipConfigurations(?:\.\d+|\[\d+\]).properties.privateIPAddress'},
    'microsoft.network/virtualnetworks': {r'properties.subnets(?:\.\d+|\[\d+\]).type'},
    'microsoft.recoveryservices/vaults/backupfabrics/protectioncontainers/protecteditems': {
        r'properties.healthDetails', r'properties.lastBackupStatus', r'properties.policyType',
        r'properties.protectionState', r'properties.protectionStatus', r'properties.softDeleteRetentionPeriod',
        r'properties.lastBackupTime', r'properties.extendedInfo', r'properties.isScheduledForDeferredDelete',
        r'properties.deferredDeleteTimeInUTC'},
    'microsoft.recoveryservices/vaults/backupfabrics/protectioncontainers': {
        r'properties.acquireStorageAccountLock', r'properties.friendlyName', r'properties.healthStatus',
        r'properties.protectableObjectType', r'properties.protectedItemCount', r'properties.registrationStatus'}
}


def resolve(value, parameters):
    if isinstance(value, str):
        match = re.fullmatch(r"\[parameters\('([A-Za-z][A-Za-z0-9]*)'\)\]", value)
        if match and match[1] in parameters: return parameters[match[1]]
    if isinstance(value, dict): return {k: resolve(v, parameters) for k, v in value.items()}
    if isinstance(value, list): return [resolve(v, parameters) for v in value]
    return value


def meaningful_changes(result, parameters):
    changes = []
    for original in result.get('changes', []):
        if original.get('changeType') in ['NoChange', 'Ignore']: continue
        item = copy.deepcopy(original)
        before, after = item.get('before') or {}, item.get('after') or {}
        kind = (after.get('type') or before.get('type') or '').lower()
        def prune(delta, prefix=''):
            path = '.'.join(v for v in [prefix, delta['path']] if v)
            if delta['propertyChangeType'] == 'NoEffect': return None
            if delta.get('children'):
                delta['children'] = [v for child in delta['children'] if (v := prune(child, path)) is not None]
                return delta if delta['children'] else None
            old, new = resolve(delta.get('before'), parameters), resolve(delta.get('after'), parameters)
            if old == new: return None
            if kind == 'microsoft.storage/storageaccounts/fileservices/shares' and path == 'properties.metadata' and old in [None, {}] and new in [None, {}]: return None
            if kind == 'microsoft.recoveryservices/vaults/backupfabrics/protectioncontainers/protecteditems' and path == 'properties.isInlineInquiry' and old is None and new is True: return None
            if delta['propertyChangeType'] == 'Delete' and any(re.fullmatch(pattern, path)
                    for pattern in READ_ONLY.get(kind, set())): return None
            return delta
        if item.get('changeType') == 'Modify':
            item['delta'] = [v for d in item.get('delta') or [] if (v := prune(d)) is not None]
            if not item['delta']: continue
        changes.append(item)
    return changes
