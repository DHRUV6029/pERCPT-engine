arr= [1,2,3]


prefix = [arr[0]]

for i in range(1 , len(arr)):
    prefix.append(prefix[-1] + arr[i])
    

ans = [0]

for i in range(1, len(arr)):
    ans.append((i * arr[i]) - prefix[i-1])
    
print(ans)