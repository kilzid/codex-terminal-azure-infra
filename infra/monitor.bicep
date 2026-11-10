param prefix string
param vmId string
@secure()
param contactEmail string
param memoryPercent int = 15
param cpuCredits int = 10

resource group 'Microsoft.Insights/actionGroups@2023-01-01' = {
  name: '${prefix}-alerts'
  location: 'global'
  properties: {
    groupShortName: 'CodexAlerts'
    enabled: true
    emailReceivers: [{ name: 'owner', emailAddress: contactEmail, useCommonAlertSchema: true }]
  }
}
resource alerts 'Microsoft.Insights/metricAlerts@2018-03-01' = [for item in [
  { name: 'memory', metric: 'Available Memory Percentage', threshold: memoryPercent }
  { name: 'credits', metric: 'CPU Credits Remaining', threshold: cpuCredits }
  { name: 'availability', metric: 'VmAvailabilityMetric', threshold: 1 }
]: {
  name: '${prefix}-vm-${item.name}'
  location: 'global'
  properties: {
    description: 'Personal Codex VM: low ${item.name}. Inspect health or resize during an idle window.'
    enabled: true
    severity: 2
    scopes: [vmId]
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    autoMitigate: true
    criteria: {
      'odata.type': 'Microsoft.Azure.Monitor.SingleResourceMultipleMetricCriteria'
      allOf: [{
        name: item.name
        criterionType: 'StaticThresholdCriterion'
        metricNamespace: 'Microsoft.Compute/virtualMachines'
        metricName: item.metric
        timeAggregation: 'Average'
        operator: 'LessThan'
        threshold: item.threshold
      }]
    }
    actions: [{ actionGroupId: group.id }]
  }
}]
