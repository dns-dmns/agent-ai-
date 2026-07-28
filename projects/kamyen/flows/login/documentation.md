# Flow Documentation: Login

## Overview
This flow verifies the user login capability on the OrangeHRM web application. The recorded flow demonstrates an initial failed attempt due to a typo in the password followed by a successful authentication sequence.

## Step-by-step Flow Sequence:
1. **Navigate to Login Page**: Target URL `https://opensource-demo.orangehrmlive.com/web/index.php/auth/login`
2. **Input Username**: Types `Admin` into `input[name="username"]`
3. **Input Incorrect Password**: Types `asmin123` into `input[name="password"]`
4. **Click Submit**: Clicks `button:has-text("Login")` (Authentication fails)
5. **Re-enter Username**: Focuses and inputs `Admin` into `input[name="username"]`
6. **Re-enter Valid Password**: Focuses and inputs `admin123` into `input[name="password"]`
7. **Click Submit**: Clicks `button:has-text("Login")` (Authentication succeeds)
8. **Redirect**: Navigates to Dashboard module.